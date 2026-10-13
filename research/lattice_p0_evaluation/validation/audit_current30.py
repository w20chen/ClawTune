import gzip
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import predict_call_load
from command_parser import ParseContext, default_service
from command_parser.service import clause_features, observation_features
from offline.runner import _safe_recorded_clause
from scripts.command_rules.audit_duration_errors import Capture, EmptyRuntime
from tool_resource.runtime_kb import ClauseResourceKB, ToolCallQuery, is_pipeline_dependent_consumer
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.resource_lattice import nonnegative

root = Path('/mnt/c/Users/29068/Desktop/ClawTune')
out = root / 'research/lattice_p0_evaluation'
dataset = Path('/mnt/c/Users/29068/Desktop/swe-rebench-original-flat-644-20260904/swe-rebench-original-flat-644-20260904')
index = {c['id']: c for c in json.loads((root / '.runtime/command-rules/duration-audit/index.json').read_bytes())['calls']}
top = json.loads((out / 'new30.json').read_bytes())
provenance = json.loads((out / 'provenance.json').read_bytes())
for name, hashes in provenance['code_sha256'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == hashes['after'], name
kb = LatticeTimeKB.from_json_obj(json.loads((root / '.runtime/lattice-p0/after/seed/clause-lattice-time-kb.json').read_bytes()))
kb.freeze()
state = kb._resource_states['load:duration_ms']
training = [(o, observation_features(o).features) for o in kb._observations
            if nonnegative(o.latency_ms) and not o.in_loop and not o.in_subst and not is_pipeline_dependent_consumer(o)]
nodes, records = {}, []
for rank, c in enumerate(top, 1):
    original = index[c['id']]
    capture = Capture(kb)
    recorded = tuple({k: raw.get(k) for k in ('argv', 'in_loop', 'in_subst', 'in_pipe', 'pipeline_position', 'stdin_from_pipe')}
        | {'bin': raw['argv'][0].rsplit('/', 1)[-1]} for raw in original['raw']['clauses'] if raw.get('argv'))
    _, diagnostics = predict_call_load(runtime=EmptyRuntime(), trie=ClauseResourceKB(), lattice=capture,
        query=ToolCallQuery(c['repo'], c['tool'], c['command'], 1e12),
        edges=load_bucket_edges((100, 500, 2000, 10000)), parsed_clauses=_safe_recorded_clause(c['command'], recorded))
    predicted = diagnostics.backends['lattice'].targets['duration_ms']
    assert math.isclose(predicted.p50, c['after']['p50'], rel_tol=1e-10, abs_tol=1e-6), (c['id'], predicted.p50)
    details = []
    retained = [cl for cl in capture.clauses if not is_pipeline_dependent_consumer(cl)]
    for cl, evidence in zip(retained, capture.rows, strict=True):
        feature = clause_features(cl, c['repo'])
        parsed = default_service().parser.parse(cl.get('syntax_argv', cl['argv']), ParseContext(repo=c['repo']))
        row = evidence.get('duration_ms')
        detail = dict(argv=cl['argv'], features=feature.to_dict(), parse_diagnostics=parsed.diagnostics)
        if row and row.get('values'):
            selected = frozenset(row['context'][1:])
            node = state.nodes[selected]
            own = selected == feature.features and bool(node.own_durations)
            key = json.dumps([sorted(selected), own])
            if key not in nodes:
                samples = [(o,f) for o,f in training if (f == selected if own else selected <= f)]
                actual = Counter(round(o.latency_ms, 5) for o,f in samples)
                expected = Counter(round(v, 5) for v in row['values'])
                assert actual == expected, (cl['argv'], len(samples), len(row['values']))
                groups = defaultdict(list)
                for o, f in samples:
                    groups[(o.repo, o.argv)].append(o.latency_ms)
                nodes[key] = dict(features=sorted(selected), uses_own=own, n=len(samples),
                    signature_count=len({f for o,f in samples}),
                    groups=[dict(repo=repo, argv=argv, durations_ms=values) for (repo,argv),values in groups.items()])
            detail.update(node=key, selected_features=sorted(selected), uses_own=own, n=len(row['values']),
                p50_ms=statistics.median(row['values']), p90_ms=sorted(row['values'])[math.ceil(.9*len(row['values']))-1],
                dropped_features=sorted(feature.features-selected),
                seconds_range=[min(row['values'])/1000,max(row['values'])/1000])
        details.append(detail)
    rawfile = dataset / c['source_file']
    with rawfile.open(encoding='utf8') as stream:
        raw = next(json.loads(line) for n,line in enumerate(stream,1) if n == c['source_line'])
    data = raw['data']
    records.append(dict(rank=rank, id=c['id'], actual_ms=c['actual_ms'], prediction=predicted.model_dump(),
        command=c['command'], source_file=c['source_file'], source_line=c['source_line'], file_sha256=hashlib.sha256(rawfile.read_bytes()).hexdigest(),
        output_tail=str(data.get('tool_result',data.get('output','')))[-6000:],
        recorded_clauses=original['raw']['clauses'], clauses=details))
    print(rank,c['id'], 'actual',round(c['actual_ms']/1000,2),'pred',round(predicted.p50/1000,2),
        [(d['argv'][:4],d.get('uses_own'),d.get('n'),d['parse_diagnostics']) for d in details],flush=True)
(out / 'current30_evidence.json.gz').write_bytes(gzip.compress(json.dumps(dict(calls=records,nodes=nodes),ensure_ascii=False).encode(),mtime=0))
print('Verified',len(records),'calls and',len(nodes),'node distributions',flush=True)
