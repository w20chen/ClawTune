"""One duration correction model for offline replay and live predictions.

First use sufficiently supported command/context evidence, then apply the
conservative early-failure prior. The underlying KB algorithms are unchanged.
"""
import ast
import json
import math
import statistics
from collections import Counter, defaultdict

from .history import finite, probe

SNAPSHOT_NAME = "execution-context-kb.json"
FORMAT = "clawtune.execution-context-kb.v1"


def stable_task(task):
    """Normalize the old live ((gateway, runtime, session), run) encoding.

    Offline dataset task IDs and new canonical IDs are left intact. If an old
    record has neither a runtime nor a provable session, retain its observation
    but do not count it as evidence of an independent task.
    """
    if not task.startswith('(('):
        return task
    try:
        value = ast.literal_eval(task)
    except (ValueError, SyntaxError):
        return task
    if not (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[0], tuple) and len(value[0]) == 3
            and all(v is None or isinstance(v, str) for v in (*value[0], value[1]))):
        return task
    (gateway, runtime, session_or_run), run_or_session = value
    if runtime:
        return json.dumps(['runtime', gateway, runtime], separators=(',', ':'))
    if session_or_run and session_or_run != run_or_session:
        return json.dumps(['session', gateway, session_or_run], separators=(',', ':'))
    return None


def independent_tasks(group):
    return {task for sample in group if (task := stable_task(sample['task'])) is not None}


def context_keys(repo, state, clauses):
    evidence_key = failure_key = None
    parsed, snapshots = state.get('parsed'), state.get('snapshots', [])
    if len(clauses) == 1 and parsed and len(parsed) == len(snapshots) == 1:
        ctx, snap, clause = parsed[0], snapshots[0], clauses[0]
        feature = clause.get('command_features')
        if (not (ctx['family'] == 'python' and probe(ctx) is None)
                and feature and not feature.get('omitted_features') and feature.get('status') != 'invalid'
                and not any(clause.get(k) for k in ('in_pipe', 'in_loop', 'in_subst', 'prediction_unavailable_reason'))
                and clause.get('syntax_argv', clause['argv']) == snap['argv']):
            # cwd is already represented by the declared context. Project repo
            # identically in replay and live requests, without duplicating cwd.
            canonical = sorted(f for f in feature['features']
                               if not f.startswith('context:') or f.startswith('context:["repo",'))
            facts = [[f[k] for k in ('kind', 'family', 'value', 'reason', 'same_argv')]
                     for f in snap['facts']]
            evidence_key = json.dumps([repo, canonical, snap['declared'], facts], sort_keys=True)
    failure = state.get('failure')
    if failure is not None:
        f = failure['features']
        failure_key = json.dumps([repo, *[f[k] for k in (
            'entry', 'module', 'cwd', 'env', 'operation', 'last_setup', 'last_same_runner')]],
            separators=(',', ':'))
    return {'evidence': evidence_key, 'failure': failure_key}


class DurationContextKB:
    def __init__(self):
        self.samples = {}
        self.groups = {'evidence': defaultdict(list), 'failure': defaultdict(list)}

    def observe(self, keys, *, ident, task, duration_ms, failure=None):
        if (ident in self.samples or not task or not finite(duration_ms) or duration_ms < 0
                or not any(keys.values())):
            return False
        sample = {'id': ident, 'task': task, 'duration_ms': float(duration_ms),
                  'failure': failure, 'keys': dict(keys)}
        self.samples[ident] = sample
        for kind, key in keys.items():
            if key is not None:
                self.groups[kind][key].append(sample)
        return True

    def predict(self, keys, baseline_ms):
        if baseline_ms is None:
            return None
        selected, reason, probability = [], None, None
        group = self.groups['evidence'].get(keys.get('evidence'), [])
        if len(group) >= 3 and len(independent_tasks(group)) >= 2:
            selected, reason = group, 'command_evidence'
            baseline_ms = statistics.median(s['duration_ms'] for s in group)
        group = self.groups['failure'].get(keys.get('failure'), [])
        counts = Counter(s['failure'] for s in group if s['failure'])
        if counts and len(group) >= 5 and len(independent_tasks(group)) >= 3:
            label, n = counts.most_common(1)[0]
            failures = [s for s in group if s['failure'] == label]
            if n / len(group) >= .98 and statistics.median(s['duration_ms'] for s in failures) < baseline_ms:
                selected, reason, probability = failures, 'early_failure:' + label, n / len(group)
        if not selected:
            return None
        values = sorted(s['duration_ms'] for s in selected)
        return {'values': values, 'p50': statistics.median(values),
                'p90': values[math.ceil(.9 * len(values)) - 1],
                'reason': reason, 'failure_fraction': probability,
                'evidence_ids': [s['id'] for s in selected]}

    def to_json_obj(self):
        return {'schema': FORMAT, 'samples': list(self.samples.values())}

    @classmethod
    def from_json_obj(cls, value):
        from clawtune_kb.contracts import validate
        validate(value, 'execution-context-kb.schema.json')
        result = cls()
        for sample in value['samples']:
            if not result.observe(sample['keys'], ident=sample['id'], task=stable_task(sample['task']) or sample['task'],
                                  duration_ms=sample['duration_ms'], failure=sample['failure']):
                raise ValueError('duplicate or invalid execution-context observation')
        return result


def correct_call(call, kb, keys):
    """Update the complete call-duration distribution, retaining raw clause diagnostics."""
    original = call.targets['duration_ms']
    correction = kb.predict(keys, original.p50)
    if correction is None:
        return call
    from clawtune_sidecar.predictors.call_load import summarize
    estimate = summarize('duration_ms', original.buckets.edges, original.backend,
                         correction['values'], context=[*original.context,
                         'execution_context:' + correction['reason']],
                         assumptions=[*original.assumptions,
                         'duration_corrected_from_pre_call_context',
                         'context_samples_are_completed_tool_durations'])
    return call.model_copy(update={'targets': {**call.targets, 'duration_ms': estimate},
                                  'quantile_method': 'median_p50_nearest_rank_p90'})
