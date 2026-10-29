"""Differential checks against full Lattice builds."""
import copy
import hashlib
import random
from dataclasses import asdict, replace

import pytest

from command_parser.models import CommandFeatures
from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB, _build_node_state


def row(i, features, *, core=(), tool='python', **kw):
    core = frozenset((f'tool={tool}', *core))
    record = CommandFeatures(tool, core | frozenset(features), core, hashlib.sha256(str(i).encode()).hexdigest())
    return ClauseObservation('r', tool, (tool, 'run.py'), i, i + .5,
        latency_ms=float(i + 20) * 17, cpu_peak_cores=float(i % 4),
        command_features=record.to_dict(), **kw)


def state(kb):
    kb._ensure_nodes()
    return (kb._nodes, kb._global_log_var, kb._global_log_std, kb._global_median_s,
            kb._resource_states, kb._query_index)


def assert_state(actual, expected):
    def nodes(a, b):
        assert list(a) == list(b)
        for key in a:
            assert asdict(a[key]) == asdict(b[key]), (key, asdict(a[key]), asdict(b[key]))
    nodes(actual[0], expected[0])
    assert actual[1:4] == expected[1:4]
    assert list(actual[4]) == list(expected[4])
    for name in actual[4]:
        a, b = actual[4][name], expected[4][name]
        assert (a.global_log_std, a.global_log_var) == (b.global_log_std, b.global_log_var)
        nodes(a.nodes, b.nodes)


@pytest.mark.parametrize('maximum', [0, 1, 2, 6])
@pytest.mark.parametrize('coverage', [False, True])
def test_every_increment_matches_full_build_with_new_nodes_and_late_samples(maximum, coverage, monkeypatch):
    rng = random.Random(817)
    rows = [row(0, ['a', 'b', 'c'], core=['a']), row(1, ['x'], tool='git')]
    kb = LatticeTimeKB.fit(rows, max_optional_features=maximum, subset_coverage=coverage)
    kb.prepare()
    original = copy.deepcopy(kb.to_json_obj())
    successor = kb.fork_for_update()
    # Expanding/changing cores can create a node that must absorb OLD samples.
    incoming = [row(2, ['a', 'b']), row(3, ['a', 'b', 'c', 'd']), row(-1, ['a'])]
    incoming += [row(i, rng.sample(list('abcdef'), rng.randrange(1, 6)),
                     core=['a'] if i % 3 == 0 else (), tool='git' if i % 5 == 0 else 'python',
                     in_loop=(i % 4 == 0)) for i in range(4, 20)]
    incoming += [replace(row(21, ['z']), latency_ms=0, cpu_peak_cores=0),
                 replace(row(22, ['z']), latency_ms=None, cpu_ns_cumulative=2e9)]
    calls = []
    import tool_time.lattice_kb as module
    monkeypatch.setattr(module, '_build_node_state', lambda *a, **k: calls.append(1))
    for extra in incoming:
        assert successor.observe_completed_clause(extra)
        successor.prepare()
        successor._advance(100 + len(rows))
        rows.append(extra)
        expected = _build_node_state(rows, max_optional_features=maximum, subset_coverage=coverage)
        assert_state(state(successor), expected)
    assert calls == []
    assert kb.to_json_obj() == original
    assert_state(state(kb), _build_node_state(rows[:2], max_optional_features=maximum, subset_coverage=coverage))


def test_resource_partitions_pending_boundaries_and_snapshot_roundtrip():
    rows = [row(0, ['a']), row(1, ['b'])]
    extra = replace(row(2, ['a']), memory_total_peak_bytes=1024, memory_extra_peak_bytes=1024,
                    memory_baseline_bytes=0, memory_eligible=True, memory_environment_id='env',
                    memory_measurement='cgroup_v2_environment_union_v1')
    later = replace(row(4, ['c']), cpu_ns_cumulative=0, sampled_peak_rss_mb=0)
    kb = LatticeTimeKB.fit(rows)
    kb.prepare()
    for r in (extra, later):
        kb.observe_completed_clause(r)
    kb.prepare()
    kb._advance(extra.ts_end)
    assert_state(state(kb), _build_node_state(rows))
    kb._advance(extra.ts_end + .1)
    assert_state(state(kb), _build_node_state([*rows, extra]))
    kb._advance(later.ts_end + .1)
    assert_state(state(kb), _build_node_state([*rows, extra, later]))
    restored = LatticeTimeKB.from_json_obj(kb.to_json_obj())
    restored.prepare()
    assert_state(state(restored), state(kb))


def test_repeated_pending_prepares_reuse_previous_delta_without_early_visibility(monkeypatch):
    kb = LatticeTimeKB.fit([row(0, ['a'])])
    kb.prepare()
    kb.observe_completed_clause(row(1, ['b']))
    kb.prepare()
    previous = kb._prepared_maintenance
    kb.observe_completed_clause(row(2, ['c']))
    kb.prepare()
    assert len(kb._prepared_maintenance.rows) == 3
    assert len(previous.rows) == 2
    assert kb.observation_count == 1
    kb._advance(1.5)  # strict ts_end boundary still excludes this completion
    assert kb.observation_count == 1
    kb._advance(5)
    assert_state(state(kb), _build_node_state([row(0, ['a']), row(1, ['b']), row(2, ['c'])]))


def test_global_prior_is_current_on_first_query_without_eager_graph_rewrite():
    rows = [row(0, ['a', 'b']), row(1, ['x'])]
    kb = LatticeTimeKB.fit(rows)
    kb.prepare()
    extra = replace(row(2, ['other']), latency_ms=1e8)
    kb.observe_completed_clause(extra)
    kb.prepare()
    kb._advance(20)
    nodes = kb._nodes
    assert not nodes.resolved
    # Resolve in reverse order to force dependency resolution, not lucky
    # parent-first iteration. Every scalar still equals the eager oracle.
    expected = _build_node_state([*rows, extra])
    for key in reversed(list(nodes)):
        assert asdict(nodes[key]) == asdict(expected[0][key])
    assert_state(state(kb), expected)


def test_exact_own_sample_query_retains_current_cold_start_flag():
    from command_parser.service import observation_features
    from tool_time.query import query_nodes

    initial = row(0, ['a'])
    kb = LatticeTimeKB.fit([initial])
    kb.prepare()
    for extra in (row(1, ['b']), row(2, ['only'], tool='git')):
        kb.observe_completed_clause(extra)
    kb.prepare()
    kb._advance(10)
    expected = _build_node_state([initial, row(1, ['b']), row(2, ['only'], tool='git')])
    for sample in (initial, row(2, ['only'], tool='git')):
        query = observation_features(sample)
        actual = query_nodes(kb._nodes, query, global_log_std=kb._global_log_std)
        reference = query_nodes(expected[0], query, global_log_std=expected[2])
        assert {k: asdict(v) for k, v in actual.items()} == {k: asdict(v) for k, v in reference.items()}
    assert not kb._nodes.resolved


def test_heap_publication_does_not_force_a_rebuild_on_the_following_batch(monkeypatch):
    import tool_time.lattice_kb as module
    rows = [row(0, ['a'])]
    kb = LatticeTimeKB.fit(rows)
    kb.prepare()
    for i in (7, 3, 6, 1, 8, 2, 5):
        extra = row(i, [str(i)])
        rows.append(extra)
        kb.observe_completed_clause(extra)
    kb.prepare()
    kb._advance(10)
    monkeypatch.setattr(module, '_build_node_state', lambda *a, **kw: pytest.fail('rebuilt after heap publication'))
    extra = row(20, ['new'])
    kb.observe_completed_clause(extra)
    kb.prepare()
    kb._advance(30)
    assert_state(state(kb), _build_node_state([*rows, extra]))


def test_new_nodes_follow_chronological_generation_order_inside_a_heap_batch():
    initial = row(0, ['a', 'b', 'c'], core=['a', 'b', 'c'])
    incoming = [row(3, ['a', 'b', 'c'], core=['a']),
                row(1, ['a', 'b', 'c'], core=['a', 'b']),
                row(2, ['a', 'b', 'c'], core=['a', 'c'])]
    kb = LatticeTimeKB.fit([initial])
    kb.prepare()
    for extra in incoming:
        kb.observe_completed_clause(extra)
    kb.prepare()
    kb._advance(10)
    assert_state(state(kb), _build_node_state([initial, *incoming]))
