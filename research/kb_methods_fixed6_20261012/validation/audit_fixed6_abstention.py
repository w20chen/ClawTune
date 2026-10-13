"""Reconstruct only the relevant generated feature nodes, without statistics."""
import gzip
import json
import math
from pathlib import Path
from command_parser.models import CommandFeatures, ParseContext
from command_parser.service import default_service
from tool_resource.runtime_kb import is_pipeline_dependent_consumer
from tool_time._lattice_vendor.features import generate_context_nodes
from tool_time.resource_lattice import ResourceState

out=Path('research/kb_methods_fixed6_20261012')
snapshot=json.loads(Path('.runtime/offline/kb-methods-fixed6-20261012-r2/seed/clause-lattice-time-kb.json').read_bytes())
cases=json.loads(gzip.decompress(Path('research/kb_methods_evaluation_20261012/shrinkage_worst30/cases.json.gz').read_bytes()))
case=next(c for c in cases if c['id']=='swe-rebench:PennyLaneAI__pennylane-6107:26')
record=case['clauses'][0]
query=CommandFeatures.from_dict(record['features'])
parsed=default_service().argv(record['argv'],ParseContext(repo='swe-rebench:PennyLaneAI/pennylane'))
assert parsed.features==query.features and parsed.core==query.core
nodes=set()
eligible=0
for row in snapshot['observations']:
    value=row['latency_ms']
    if (row['in_loop'] or row['in_subst'] or is_pipeline_dependent_consumer(row)
        or not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<0):
        continue
    eligible+=1
    f=CommandFeatures.from_dict(row['command_features'])
    if not f.core<=query.features:
        continue
    # Enumerate precisely those partial nodes that can pass this query.
    for node in generate_context_nodes(f.features & query.features,f.core,max_optional_features=6,always_keep_exact=False):
        if query.core<=node:
            nodes.add(node)
    if query.core<=f.features<=query.features:
        nodes.add(f.features)
assert query.features not in nodes
result=ResourceState({f:None for f in nodes},1.0,1.0).predict(
    'swe-rebench:PennyLaneAI/pennylane',record['argv'],'duration_ms','shrinkage',command_features=query)
assert result.unavailable_reason=='lattice_candidate_limit_exceeded'
audit=dict(id=case['id'],command=case['command'],actual_ms=case['actual_ms'],
    eligible_duration_observations=eligible,legal_candidate_nodes=len(nodes),candidate_limit=512,
    exact_node_exists=False,reason=result.unavailable_reason)
(out/'abstention_audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps(audit,ensure_ascii=False,indent=2))
