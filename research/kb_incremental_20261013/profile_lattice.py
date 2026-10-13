"""Diagnostic only: profile maintenance separately from initialization."""
import cProfile
import pstats

from research.kb_online_cost_20261012 import study

original = study.LatticeTimeKB.prepare

def prepare(kb):
    if not kb.pending_count:
        return original(kb)
    profile = cProfile.Profile()
    profile.enable()
    original(kb)
    profile.disable()
    print('PENDING', kb.pending_count, flush=True)
    pstats.Stats(profile).sort_stats('cumulative').print_stats(32)

study.LatticeTimeKB.prepare = prepare
study.main()
