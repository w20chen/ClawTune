"""Summary reuse preserves mutable-input semantics and independent explanations."""
import math
from unittest.mock import patch

import pytest

from clawtune_sidecar.predictors import call_load


def estimate(samples, cache=None, edges=(100, 500), **kwargs):
    return call_load.summarize('duration_ms', edges, 'lattice', samples, cache=cache, **kwargs)


def test_tuple_hit_skips_sample_scan_and_content_lookup():
    samples = tuple(i + .5 for i in range(2048))
    cache = call_load.CompositionCache()
    expected = estimate(samples, cache)

    class NoLookup(dict):
        def get(self, *args):
            raise AssertionError('identity hit must not hash the sample contents')

    cache.summaries = NoLookup(cache.summaries)
    original = math.isfinite
    def check_only_edges(value):
        assert value in (100, 500), 'identity hit must not rescan samples'
        return original(value)
    with patch.object(call_load.math, 'isfinite', check_only_edges):
        assert estimate(samples, cache) == expected
    assert cache.sample_summaries[(id(samples), (100, 500))][0] is samples


def test_distinct_equal_batches_keep_content_reuse():
    first = tuple(float(i) for i in range(20))
    second = tuple(list(first))
    assert first is not second
    cache = call_load.CompositionCache()
    expected = estimate(first, cache)
    with patch.object(call_load.statistics, 'mean', side_effect=AssertionError('recomputed mean')):
        assert estimate(second, cache) == expected
    assert len(cache.summaries) == 1
    assert len(cache.sample_summaries) == 2


def test_identity_reuse_separates_edges_metadata_and_output_mutations():
    samples = (0., 100., 500., 1000.)
    cache = call_load.CompositionCache()
    first = estimate(samples, cache)
    first.buckets.probabilities[0] = 0
    first.context.append('changed')
    options = dict(method='composed', evidence_counts=[2, 3], context=['new'], assumptions=['new'])
    assert estimate(samples, cache, **options) == estimate(samples, **options)
    other_target = call_load.summarize('cpu_time_seconds', (100, 500), 'trie', samples, cache=cache)
    assert other_target == call_load.summarize('cpu_time_seconds', (100, 500), 'trie', samples)
    edges = [100, 500]
    estimate(samples, cache, edges=edges)
    edges[0] = 200
    assert estimate(samples, cache, edges=edges) == estimate(samples, edges=edges)
    assert len(cache.sample_summaries) == 2


def test_mutable_list_is_revalidated_after_changes():
    samples = [1., 3.]
    cache = call_load.CompositionCache()
    assert estimate(samples, cache).p50 == 2
    samples[:] = [5., 9., float('nan'), -1., True, None]
    result = estimate(samples, cache)
    assert result == estimate(samples)
    assert (result.p50, result.sample_count) == (7, 2)
    samples.clear()
    assert estimate(samples, cache).status == 'unavailable'
    assert not cache.sample_summaries


def test_tuple_with_mutable_numeric_subclass_is_revalidated():
    class MutableFloat(float):
        def __float__(self):
            return self.current

    value = MutableFloat(1)
    value.current = 1.
    samples = (value, 3.)
    cache = call_load.CompositionCache()
    assert estimate(samples, cache).p50 == 2
    value.current = 9.
    assert estimate(samples, cache) == estimate(samples)
    assert estimate(samples, cache).p50 == 6
    assert not cache.sample_summaries


def test_tuple_subclass_with_mutable_iteration_is_revalidated():
    class MutableTuple(tuple):
        def __iter__(self):
            return iter(self.current)

    samples = MutableTuple((1., 3.))
    samples.current = [1., 3.]
    cache = call_load.CompositionCache()
    assert estimate(samples, cache).p50 == 2
    samples.current = [5., 9.]
    assert estimate(samples, cache) == estimate(samples)
    assert estimate(samples, cache).p50 == 7
    assert not cache.sample_summaries


@pytest.mark.parametrize('samples', [(), (-1., float('nan'), float('inf')),
                                    (0., 1., -1., float('nan'), float('inf'))])
def test_filtered_tuple_counts_and_unavailable_reasons_stay_fresh(samples):
    cache = call_load.CompositionCache()
    for reason in ('first', 'second'):
        assert estimate(samples, cache, reason=reason) == estimate(samples, reason=reason)


def test_generated_draws_are_shared_immutable_batches():
    cache = call_load.CompositionCache()
    values, groups = [(1., 2.), (3., 4.)], [[0], [1]]
    first = call_load._draw_samples('duration_ms', values, groups, cache=cache)
    second = call_load._draw_samples('duration_ms', values, groups, cache=cache)
    assert type(first) is tuple and first is second
    assert first == call_load._draw_samples('duration_ms', values, groups)
    estimate(first, cache)
    assert cache.sample_summaries[(id(first), (100, 500))][0] is first
    # The cache retains this result only for its own request.
    other_cache = call_load.CompositionCache()
    assert estimate(first, other_cache) == estimate(first)
    assert len(other_cache.sample_summaries) == 1
