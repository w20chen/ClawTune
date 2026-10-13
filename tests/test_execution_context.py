"""Causal replay, statistical guards, persistence and live/offline parity."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/sidecar/src'))
from execution_context.history import (ContextHistory, contexts, observations, replay,
                                       failure_snapshot, output_text)
from execution_context.duration import DurationContextKB, context_keys, correct_call

FAILURE = "ImportError while loading conftest '/testbed/tests/conftest.py'.\nModuleNotFoundError: No module named 'pkg_resources'\nExit code: 4"
PASSED = '================ 15 passed in 2.30s ================\nExit code: 0'


def event(n,start,end,command='python -m pytest',output='',tool='exec'):
    return {'id':str(n),'line':n,'start':start,'end':end,'tool':tool,
            'args':{'command':command,'working_dir':'/testbed'},'output':output}


class EvidenceTests(unittest.TestCase):
    def test_quoted_python_code_is_not_shell_control_flow(self):
        parsed = contexts({'command': 'python -c "import sys; print(sys.version)"'})
        self.assertEqual(len(parsed), 1)
        for command in ('python -V | cat', 'python -V && $(echo python) -V', 'python -V > a', 'python -V &', 'python -V\npython -V'):
            self.assertIsNone(contexts({'command': command}))

    def test_unique_version_in_diagnostic_bundle(self):
        rows = [event(1, 1, 2, 'python --version; which python', 'Python 3.11.2\n/usr/bin/python\nExit code: 0'), event(2, 3, 4)]
        facts = replay(rows, 'case')['2'][0]['facts']
        self.assertEqual([(f['kind'], f['value']) for f in facts], [('python_version', '3.11.2')])
        ambiguous = contexts({'command': 'python -V; /venv/bin/python -V'})
        self.assertEqual(observations(ambiguous, 'Python 3.11.2\nPython 3.12.4'), [])

    def test_unittest_mixed_import_failures_are_not_early_failure(self):
        text = 'ImportError: Failed to import test module: tests.x\nModuleNotFoundError: No module named duckdb\nRan 660 tests in 131.895s\nFAILED (errors=1)'
        rows = [event(1, 1, 2, 'python -m unittest', text), event(2, 3, 4, 'python -m unittest'), event(3, 5, 6)]
        snapshots = replay(rows, 'case')
        self.assertEqual(snapshots['2'][0]['facts'][0]['value'], 'tests_reported_with_import_errors')
        self.assertEqual(snapshots['3'][0]['facts'], [])  # no unittest outcome copied into pytest

    def test_import_fact_is_historical_and_invalidated_by_repair(self):
        rows = [event(1, 1, 2, 'python -c "import pandas"', 'Traceback (most recent call last):\nModuleNotFoundError: No module named pandas'), event(2, 3, 4), event(3, 5, 6, 'python -m pip install pandas'), event(4, 7, 8)]
        snapshots = replay(rows, 'case')
        self.assertEqual(snapshots['2'][0]['facts'][0]['kind'], 'import_failure')
        self.assertEqual(snapshots['4'][0]['facts'], [])

    def test_mutating_command_has_pre_mutation_snapshot(self):
        rows = [event(1, 1, 2, output=FAILURE), event(2, 3, 4, 'python -m pip install setuptools'), event(3, 5, 6)]
        # A version fact is shared by interpreter identity, while runner facts
        # remain scoped to the runner family.
        rows[0] = event(1, 1, 2, 'python -V', 'Python 3.11.2')
        snapshots = replay(rows, 'case')
        self.assertEqual(snapshots['2'][0]['facts'][0]['value'], '3.11.2')
        self.assertEqual(snapshots['3'][0]['facts'], [])

    def test_current_future_equal_clock_and_overlap(self):
        rows = [event(1, 1, 2, output=FAILURE), event(2, 3, 4, output=PASSED), event(3, 5, 6)]
        before = replay(rows, 'case')
        changed = copy.deepcopy(rows)
        changed[1]['output'] = FAILURE
        self.assertEqual(before['2'], replay(changed, 'case')['2'])
        self.assertEqual(before['2'], replay(rows[:2], 'case')['2'])
        for end in (3, 4):
            self.assertEqual(replay([event(1, 1, end, output=FAILURE), event(2, 3, 5)], 'c')['2'][0]['facts'], [])
        overlap = [event(1, 1, 7, 'pip install x'), event(2, 2, 3, 'python -V', 'Python 3.11.2'), event(3, 8, 9)]
        self.assertEqual(replay(overlap, 'c')['3'][0]['facts'], [])

    def test_expiry_missing_clocks_and_scope(self):
        first = event(1, 1, 2, output=FAILURE)
        self.assertEqual(replay([first, event(2, 603, 604)], 'c')['2'][0]['facts'], [])
        rows = [first, event(2, None, None, 'pip install x'), event(3, 5, 6)]
        self.assertTrue(replay(rows, 'c')['3'][0]['history_blocked'])
        self.assertEqual(replay([event(1, 5, 6)], 'other')['1'][0]['facts'], [])

    def test_mixed_output_rejection(self):
        parsed = contexts({'command': 'python -m pip install x && python -m pytest'})
        self.assertEqual(observations(parsed, PASSED), [])


class ContextModelTests(unittest.TestCase):
    def test_live_and_trace_history_match_through_repair_and_recovery(self):
        rows = [event(1, 1, 2, output='python: No module named pytest\nExit code: 1'),
                event(2, 3, 4, 'python -m pip install pytest', 'Exit code: 0'),
                event(3, 5, 6, output=PASSED), event(4, 7, 8),
                event(5, 9, 10, '/venv/bin/python -m pytest')]
        evidence = replay(rows, 'scope')
        live = ContextHistory('scope')
        for row in rows:
            current = live.begin(row)
            self.assertEqual(current['snapshots'], evidence.get(row['id'], []))
            self.assertEqual(current['failure'], failure_snapshot(row, rows))
            live.complete(row)
        self.assertEqual(ContextHistory('other').begin(rows[-1])['failure']['features']['last_same_runner'], 'unknown')

    def test_failure_gate_and_snapshot_roundtrip(self):
        kb = DurationContextKB()
        keys = {'evidence': None, 'failure': 'group'}
        for i in range(5):
            kb.observe(keys, ident=str(i), task=str(i), duration_ms=20 + i, failure='missing_module')
        result = kb.predict(keys, 3000)
        self.assertEqual(result['p50'], 22)
        self.assertIsNone(kb.predict(keys, None))
        self.assertIsNone(kb.predict(keys, 10))
        clone = DurationContextKB.from_json_obj(kb.to_json_obj())
        self.assertEqual(clone.predict(keys, 3000), result)
        clone.observe(keys, ident='normal', task='new', duration_ms=10000)
        self.assertIsNone(clone.predict(keys, 3000))
        bad = kb.to_json_obj()
        bad['samples'] = [*bad['samples'], bad['samples'][0]]
        with self.assertRaises(ValueError):
            DurationContextKB.from_json_obj(bad)

    def test_independent_task_support_and_evidence_then_failure(self):
        kb = DurationContextKB()
        keys = {'evidence': 'exact', 'failure': 'broad'}
        for i in range(5):
            kb.observe(keys, ident=str(i), task='one-task', duration_ms=20, failure='missing_module')
        self.assertIsNone(kb.predict(keys, 3000))
        kb.observe(keys, ident='6', task='second', duration_ms=20, failure='missing_module')
        self.assertEqual(kb.predict(keys, 3000)['reason'], 'command_evidence')
        for i in range(7, 13):
            kb.observe({'evidence': 'other', 'failure': 'broad'}, ident=str(i), task=str(i), duration_ms=10, failure='missing_module')
        self.assertEqual(kb.predict(keys, 3000)['p50'], 15)
        self.assertTrue(kb.predict(keys, 3000)['reason'].startswith('early_failure:'))

    def test_all_duration_statistics_change_together(self):
        from clawtune_sidecar.predictors.call_load import compose
        from clawtune_sidecar.contracts.load_prediction import CallLoadPrediction
        from clawtune_sidecar.prediction_config import load_bucket_edges
        edges = load_bucket_edges((100, 500, 2000, 10000))
        original = compose('lattice', [{'duration_ms': {'values': [3000]}}], edges)
        kb = DurationContextKB()
        keys = {'evidence': None, 'failure': 'group'}
        for i, ms in enumerate([10, 20, 30, 40, 60]):
            kb.observe(keys, ident=str(i), task=str(i), duration_ms=ms, failure='missing_module')
        result = correct_call(original, kb, keys)
        CallLoadPrediction.model_validate(result.model_dump())
        duration = result.targets['duration_ms']
        self.assertEqual((duration.p50, duration.p90, duration.avg), (30, 60, 32))
        self.assertEqual(duration.buckets.probabilities, [1, 0, 0, 0, 0])
        for target in result.targets:
            if target != 'duration_ms':
                self.assertEqual(result.targets[target], original.targets[target])

    def test_output_envelopes_and_no_current_labels_in_keys(self):
        text = output_text({'content': [{'text': 'python: No module named pytest'}], 'details': {'exitCode': 1}})
        self.assertTrue(text.endswith('Exit code: 1'))
        row = event(1, 1, 2, output=text)
        state = ContextHistory('scope').begin(row)
        keys = context_keys('repo', state, [])
        row['output'] = PASSED
        self.assertEqual(context_keys('repo', ContextHistory('scope').begin(row), []), keys)


if __name__ == '__main__':
    unittest.main()
