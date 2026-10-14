"""Compare repaired bundled rules with pre-repair HEAD rules on shell clauses.

Keeps the current parser's user-approved unknown-tool fallback on both sides.
HEAD's projection supplies the pre-repair feature budget behavior. No commands
from the dataset are executed and no models are trained.
"""
import gzip
import hashlib
import json
import subprocess
import types
from collections import Counter
from pathlib import Path

from command_parser import ParseContext
from command_parser.parser import CommandParser
from command_parser.projection import project
from command_parser.registry import RuleRegistry

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.runtime/parameter-parse-repair-20261014'
RULES = ROOT / 'services/sidecar/src/command_parser/rules'


def head(path):
    return subprocess.check_output(['git', 'show', 'HEAD:' + path], cwd=ROOT).decode('utf-8')


def main():
    baseline = RuleRegistry(bundled=False)
    for path in sorted(RULES.glob('*.json')):
        if path.name != 'manifest.json':
            baseline.add(json.loads(head(path.relative_to(ROOT).as_posix())), persist=False)
    before, after = CommandParser(baseline), CommandParser(RuleRegistry())
    old_projection = types.ModuleType('command_parser._pre_repair_projection')
    old_projection.__package__ = 'command_parser'
    exec(compile(head('services/sidecar/src/command_parser/projection.py'), '<HEAD projection>', 'exec'), old_projection.__dict__)
    source = ROOT / 'research/kb_systematic_20261013/results/calls.json.gz'
    rows = json.loads(gzip.decompress(source.read_bytes()))
    counts, transitions, regressions = Counter(), Counter(), []
    cache = {}
    for row in rows:
        if row['tool'] not in {'exec', 'terminal_exec'}:
            continue
        context = ParseContext(repo=row['repo'])
        for clause in row['clauses']:
            argv = clause.get('syntax_argv', clause['argv'])
            intents = clause.get('word_intents')
            key = json.dumps([argv, intents, row['repo']], sort_keys=True)
            if key not in cache:
                left = before.parse(argv, context, word_intents=intents)
                right = after.parse(argv, context, word_intents=intents)
                lf = old_projection.project(left, context)
                rf = project(right, context)
                cache[key] = (left.status, right.status, left.to_dict() == right.to_dict(),
                              lf.features == rf.features, lf.omitted_features == 0)
            ls, rs, parse_same, features_same, untruncated = cache[key]
            counts['clauses'] += 1
            transitions[ls + '->' + rs] += 1
            counts['feature_changes'] += not features_same
            if ls == 'complete' and rs != 'complete':
                regressions.append({'trace': row['trace'], 'argv': argv, 'before': ls, 'after': rs})
            if parse_same and untruncated:
                counts['unchanged_untruncated_parses'] += 1
                if not features_same:
                    regressions.append({'trace': row['trace'], 'argv': argv, 'error': 'unintended feature change'})
    result = {'counts': dict(counts), 'transitions': dict(transitions), 'regressions': regressions,
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'baseline_rule_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'scope': 'Shell clauses only; parse-status and feature consistency, not prediction accuracy.'}
    OUT.mkdir(exist_ok=True)
    (OUT / 'paired.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
    assert not regressions


if __name__ == '__main__':
    main()
