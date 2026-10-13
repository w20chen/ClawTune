"""Recompute generation budgets without rebuilding statistical node views."""
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from command_parser.models import CommandFeatures
from tool_resource.runtime_kb import is_pipeline_dependent_consumer
from tool_time._lattice_vendor.features import estimate_node_count, generate_context_nodes
from tool_time.lattice_kb import _effective_max_optional_features
from tool_time.resource_lattice import nonnegative

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
snapshot = json.loads((ROOT/'.runtime/offline/kb-methods-20261012/seed/clause-lattice-time-kb.json').read_bytes())
rows = [o for o in snapshot['observations'] if nonnegative(o.get('latency_ms'))
        and not o.get('in_loop') and not o.get('in_subst') and not is_pipeline_dependent_consumer(o)]
assert len(rows)==10724
features = [CommandFeatures.from_dict(o['command_features']) for o in rows]
counts = [len(f.features-f.core) for f in features]
result = dict(observations=len(rows),distinct_feature_sets=len({f.features for f in features}),
              optional_count_histogram=dict(sorted(Counter(counts).items())),budgets=[])
for maximum in range(6,-1,-1):
    estimates = [estimate_node_count(n,max_optional_features=maximum) for n in counts]
    record = dict(maximum=maximum,total_occurrences=sum(estimates),largest_per_observation=max(estimates),
                  within_both_limits=sum(estimates)<=20000 and max(estimates)<=4096)
    if maximum<=2:
        unique = {fs for f in features for fs in generate_context_nodes(f.features,f.core,max_optional_features=maximum)}
        record['distinct_generated_nodes']=len(unique)
    result['budgets'].append(record)
result['effective_maximum']=_effective_max_optional_features([SimpleNamespace(command_features=o['command_features']) for o in rows])
(OUT/'node_budget.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
print(json.dumps(result,indent=2),flush=True)
