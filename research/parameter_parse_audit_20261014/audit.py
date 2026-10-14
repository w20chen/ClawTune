"""Read-only audit of current argument parsing; never executes recorded commands.

Run with PYTHONPATH=services/sidecar/src:. python research/parameter_parse_audit_20261014/audit.py
"""
import gzip
import hashlib
import json
import shlex
import unittest
from collections import Counter, defaultdict
from pathlib import Path

from command_parser import CommandFeatureService, ParseContext
from command_parser.projection import project
from tool_time.edge_kappa_adapter import feature_query

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
SOURCE = ROOT / 'research/kb_systematic_20261013/results/calls.json.gz'
SERVICE = CommandFeatureService()


def inspect(argv, context=None, intents=None):
    context = context or ParseContext()
    parsed = SERVICE.parser.parse(argv, context, word_intents=intents)
    features = project(parsed, context)
    full = project(parsed, context, max_features=100000)
    assert feature_query(features).features == features.features
    return {'argv': argv, 'status': parsed.status, 'diagnostics': parsed.diagnostics,
            'target': parsed.target, 'layers': parsed.layers,
            'features': sorted(features.features), 'omitted': features.omitted_features,
            'dropped_features': sorted(full.features - features.features)}


def main():
    commands = [
        'python -u -m pytest -q -n 8 --tb=short tests/unit tests/integration',
        'python -m pytest -k fast -m slow tests/unit',
        'python -m pytest -m slow -k fast tests/unit',
        'python -m unittest -q tests.test_example',
        'python -m compileall -q src',
        'head -n -5 file',
        'python -m pytest --new-plugin-option value -n 8 tests/unit',
        'git -C repo status --short',
        'python -c "print(1)" -m pytest',
        'python -m pytest -n 8 ' + ' '.join('tests/test_%02d.py' % i for i in range(20)),
    ]
    probes = [{'command': command, **inspect(shlex.split(command))} for command in commands]
    # Invoke stdlib's real parser without discovering or running any tests.
    program = unittest.TestProgram.__new__(unittest.TestProgram)
    program.progName = 'python -m unittest'
    parent = program._getParentArgParser()
    expected = vars(program._getMainArgParser(parent).parse_args(['-q', 'tests.test_example']))
    (OUT / 'probes.json').write_text(json.dumps({'unittest_reference': expected, 'cases': probes},
                                              ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    rows = json.loads(gzip.decompress(SOURCE.read_bytes()))
    counts, diagnostics, tools = Counter(), Counter(), defaultdict(Counter)
    examples = defaultdict(list)
    feature_loss = Counter()
    for row in rows:
        if row['tool'] not in {'exec', 'terminal_exec'}:
            continue
        counts['shell_calls'] += 1
        counts['calls_with_extracted_clauses'] += bool(row['clauses'])
        for index, clause in enumerate(row['clauses']):
            context = ParseContext(repo=row['repo'])
            result = inspect(clause.get('syntax_argv', clause['argv']), context, clause.get('word_intents'))
            counts['clauses'] += 1
            counts[result['status']] += 1
            counts['truncated_clauses'] += bool(result['omitted'])
            counts['omitted_features'] += result['omitted']
            counts['clauses_with_opaque'] += any(layer['opaque'] for layer in result['layers'])
            tools[result['target'][0] if result['target'] else clause['bin']][result['status']] += 1
            diagnostics.update(result['diagnostics'])
            feature_loss.update(feature.split(':', 1)[0] for feature in result['dropped_features'])
            categories = [*result['diagnostics']]
            if result['omitted']:
                categories.append('truncated')
            for category in categories:
                if len(examples[category]) < 3:
                    examples[category].append({'call_id': row['id'], 'trace': row['trace'],
                                              'clause_index': index, **result})
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT / 'services/sidecar/src/command_parser').rglob('*'))
                if p.suffix in {'.py', '.json'}}
    summary = {'scope': 'Current bundled argument parser replay on previously extracted shell clauses; '
                       'not a new shell extraction, not semantic accuracy labels, not a prediction evaluation.',
               'source': str(SOURCE.relative_to(ROOT)),
               'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
               'counts': dict(counts), 'diagnostics': dict(diagnostics.most_common()),
               'by_entry': {key: dict(value) for key, value in sorted(tools.items())},
               'dropped_feature_types': dict(feature_loss), 'source_manifest': manifest}
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (OUT / 'examples.json').write_text(json.dumps(examples, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'counts': counts, 'top_diagnostics': diagnostics.most_common(15),
                      'dropped_feature_types': feature_loss}, ensure_ascii=False))


if __name__ == '__main__':
    main()
