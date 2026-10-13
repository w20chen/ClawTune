"""Record executed checks and the exact optimized sources without trace writes."""
import hashlib
import json
import platform
import zipfile
from pathlib import Path

root=Path(__file__).parent
checks={
    'python':platform.python_version(),
    'regression_command':'wsl -e bash .runtime/entry-constraint-validation/run.sh services/sidecar/tests/test_lattice_performance_equivalence.py services/sidecar/tests/test_lattice_resources.py services/sidecar/tests/test_lattice_query_policy.py services/sidecar/tests/test_lattice_time_kb.py services/sidecar/tests/test_command_normalization.py services/sidecar/tests/test_kb_entry_constraints.py tests/test_lattice_snapshot_handoff.py tests/test_lattice_oracle.py',
    'regression_passed':152,
    'integration_command':"wsl -e bash .runtime/entry-constraint-validation/run.sh services/sidecar/tests/test_call_load.py tests/test_demo_workflows.py::test_offline_lattice_variants_share_frozen_statistics_and_preserve_default_results services/sidecar/tests/test_tool_resource_predictor.py -k 'call_load or prepared or background or offline_lattice_variants'",
    'integration_passed':67,
    'diff_check':'git diff --check: passed',
    'oracle':'Unchanged; regression tests ran. Full dataset oracle experiment was intentionally not rerun.',
    'source_snapshot':{},
}
seed=Path('.runtime/offline/kb-methods-fixed6-20261012-r2/seed')
for name in ['clause-lattice-time-kb.json','edge-kappa-kb.json']:
    checks['source_snapshot'][name]=hashlib.sha256((seed/name).read_bytes()).hexdigest()
edge=json.loads((seed/'edge-kappa-kb.json').read_bytes())
checks['previous_edge_config']=edge['config']
checks['previous_edge_nodes']=len(edge['nodes'])
files=sorted(Path('services/sidecar/src/tool_time').rglob('*.py'))
files += [Path('services/sidecar/tests/test_lattice_performance_equivalence.py'),Path('offline/lattice_oracle.py')]
with zipfile.ZipFile(root/'optimized_sources.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        archive.write(path,str(path))
(root/'validation.json').write_text(json.dumps(checks,indent=2)+'\n')
print('ARCHIVED',checks['regression_passed']+checks['integration_passed'],'passing tests')
