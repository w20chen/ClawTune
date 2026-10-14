"""Reproduce specific failures separately from aggregate parse-status counts."""
import json
import shlex
from pathlib import Path

from command_parser import CommandFeatureService, ParseContext
from command_parser.projection import project

out = Path(__file__).resolve().parent
service = CommandFeatureService()
examples = json.loads((out / 'examples.json').read_text(encoding='utf-8'))
real = next(row for row in examples['truncated'] if row['target'] == ['pytest'])
argv = real['argv']
other = list(argv)
other[other.index('-n') + 1] = '1'
context = ParseContext(repo='swe-rebench:PennyLaneAI/pennylane')
left = service.argv(argv, context)
right = service.argv(other, context)
assert left.features == right.features
assert left.status == right.status == 'complete'
assert project(service.parser.parse(argv, context), context, max_features=1000).features != project(
    service.parser.parse(other, context), context, max_features=1000).features

records = []
for command in ['pip install -e . --no-deps', 'pip install -r requirements.txt',
                'head -100', 'head -n -5 file', 'git diff --check',
                'python -m unittest -q tests.test_example']:
    parsed = service.parser.parse(shlex.split(command), ParseContext())
    records.append({'command': command, 'parsed': parsed.to_dict()})

result = {'worker_collision': {'trace': real['trace'], 'call_id': real['call_id'],
                              'recorded_argv': argv, 'counterfactual_argv': other,
                              'both_status': left.status, 'same_kb_features': left.features == right.features,
                              'different_without_feature_cap': True,
                              'omitted_features': left.omitted_features},
          'reproductions': records,
          'validation': [
              {'environment': 'Windows Python 3.12', 'result': '73 passed',
               'command': "$env:PYTHONPATH='services/sidecar/src;.'; python -m pytest -p no:cacheprovider services/sidecar/tests/test_command_parser.py services/sidecar/tests/test_command_projection.py services/sidecar/tests/test_command_fallback.py -q"},
              {'environment': 'WSL, .runtime/wsl-offline-venv2/bin/python', 'result': '148 passed',
               'command': 'PYTHONPATH=services/sidecar/src:. .runtime/wsl-offline-venv2/bin/python -m pytest -p no:cacheprovider services/sidecar/tests/test_command_parser.py services/sidecar/tests/test_command_projection.py services/sidecar/tests/test_command_fallback.py services/sidecar/tests/test_command_normalization.py services/sidecar/tests/test_tool_resource_commands.py services/sidecar/tests/test_literal_command_heads.py -q --basetemp /tmp/clawtune-parser-audit-20261014'}]}
(out / 'verified-examples.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
