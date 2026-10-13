"""Verify that cross-device timings use identical source, inputs and outputs."""
import json
import math
from pathlib import Path

base = Path(__file__).resolve().parent
left, right = base / 'notebook', base / 'kunpeng'
manifests = [json.loads((p / 'source_sha256.json').read_bytes()) for p in (left, right)]
production = lambda x: {k: v for k, v in x.items() if k.startswith(('services/', 'contracts/'))}
assert production(manifests[0]) == production(manifests[1]), 'source mismatch'
environments = [json.loads((p / 'environment.json').read_bytes()) for p in (left, right)]
for name in ('numpy', 'pydantic', 'jsonschema', 'psutil'):
    versions = [{k.lower(): v for k, v in e['packages'].items()}[name] for e in environments]
    assert versions[0] == versions[1], (name, versions)
checks = []
for name in ('lattice-6', 'lattice-2', 'lattice-1', 'edge-2', 'edge-1', 'edge-cold-6', 'pair-1', 'pair-2'):
    values = [json.loads((p / (name + '.json')).read_bytes()) for p in (left, right)]
    assert all(v['phase']['stage'] == 'done' for v in values), name
    for key in ('corpus_sha256', 'inputs_sha256', 'training_rows', 'test_calls'):
        assert values[0][key] == values[1][key], (name, key)
    for key in ('duration_nodes', 'graph_nodes', 'graph_edges', 'published_added_observations'):
        if key in values[0]:
            assert values[0][key] == values[1][key], (name, key)
    rows = [[json.loads(line) for line in (p / (name + '-rows.jsonl')).read_text().splitlines()]
            for p in (left, right)]
    queries = [[v for v in r if v['kind'] == 'query'] for r in rows]
    assert len(queries[0]) == len(queries[1]), name
    for a, b in zip(*queries):
        for key in ('repeat', 'sample', 'status'):
            assert a[key] == b[key], (name, key)
        assert (a['p50'] is None and b['p50'] is None) or math.isclose(a['p50'], b['p50'], rel_tol=1e-9, abs_tol=1e-6), (name, a, b)
    checks.append({'case': name, 'matching_query_outputs': len(queries[0])})
for path in (left, right):
    stopped = json.loads((path / 'edge-6-supervisor.json').read_bytes())
    assert stopped['termination'] == 'time_budget_exceeded'
report = {'production_source_files_matched': len(production(manifests[0])),
          'core_dependency_versions_matched': True, 'checks': checks,
          'edge_6': 'Both full-corpus fits stopped at the explicit 180 second study limit; no full-model online latency inferred.'}
(base / 'verification.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
