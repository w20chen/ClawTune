import json
import statistics
from pathlib import Path

from command_parser import CommandFeatureService, ParseContext
from tool_time.lattice_kb import LatticeTimeKB

root = Path('/mnt/c/Users/29068/Desktop/ClawTune')
kb = LatticeTimeKB.from_json_obj(json.loads((root / '.runtime/lattice-p0/after/seed/clause-lattice-time-kb.json').read_bytes()))
kb.prepare()
state = kb._resource_states['load:duration_ms']
records = []
for repo, argv in [
    ('swe-rebench:tobymao/sqlglot', ['python', '-m', 'unittest']),
    ('swe-rebench:tobymao/sqlglot', ['pytest', '-q']),
    ('swe-rebench:tobymao/sqlglot', ['python', '-m', 'pytest', '-q']),
    ('swe-rebench:PennyLaneAI/pennylane', ['python', '-m', 'pytest', '-q', '-n', 'auto']),
    ('swe-rebench:PennyLaneAI/pennylane', ['python', '-m', 'pytest', '-q', 'tests/gradients']),
]:
    query = CommandFeatureService().argv(argv, ParseContext(repo=repo))
    result = state.predict(repo, argv, 'duration_ms', 'shrinkage', command_features=query)
    node = state.nodes.get(frozenset(result.selected_features))
    records.append(dict(repo=repo, argv=argv, query=query.to_dict(), prediction=result.to_dict(),
        coverage_count=len(node.durations) if node else 0,
        coverage_median_s=statistics.median(node.durations) if node else None,
        own_count=len(node.own_durations) if node else 0,
        own_median_s=statistics.median(node.own_durations) if node and node.own_durations else None))
(root / 'research/lattice_p0_evaluation/selected_sample_evidence.json').write_text(json.dumps(records, indent=2) + '\n')
for row in records:
    print(row['argv'], 'coverage', row['coverage_count'], row['coverage_median_s'], 'own', row['own_count'], row['own_median_s'], 'prediction',row['prediction'])
