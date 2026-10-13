import json, subprocess, sys
from collections import Counter
from pathlib import Path
root = Path('research/kb_algorithm_acceleration_20261012')
files = ['clawtune_sidecar/predictors/call_load.py', 'edge_kappa_kb/graph.py', 'edge_kappa_kb/kb.py', 'tool_time/_lattice_vendor/nodes.py', 'tool_time/_lattice_vendor/shrinkage.py', 'tool_time/lattice_kb.py', 'tool_time/resource_lattice.py']
findings = []
for label, base in [('before', '.runtime/kb-algorithm-before'), ('after', 'services/sidecar/src')]:
 result = subprocess.run([sys.executable, '-m', 'ruff', 'check', '--output-format', 'json', *[str(Path(base)/f) for f in files]], capture_output=True, text=True)
 assert result.returncode in (0,1), result.stderr
 rows = json.loads(result.stdout)
 (root/f'ruff-{label}.json').write_text(json.dumps(rows,indent=2))
 findings.append(Counter((str(Path(row['filename']).relative_to(Path(base).resolve())), row['code'], row['message']) for row in rows))
new = findings[1] - findings[0]
print('New findings:', list(new.elements()))
assert not new
