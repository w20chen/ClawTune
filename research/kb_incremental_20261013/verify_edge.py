"""Run unchanged event sequences with either baseline or current source on PYTHONPATH."""
import argparse
import hashlib
import json
import random
from dataclasses import asdict
from pathlib import Path

from edge_kappa_kb import EdgeKappaKB, FeatureQuery, TimeOutcome, TrainingEvent

p = argparse.ArgumentParser()
p.add_argument('--out', type=Path, required=True)
args = p.parse_args()
results = []
for maximum in (0, 2, 6):
    for shared, learn in ((False, True), (True, True), (False, False)):
        rng = random.Random(402)
        core = frozenset({'tool=t'})
        def event(i, rng=rng, core=core):
            features = core | frozenset(rng.sample(list('abcdefgh'), rng.randrange(1, 9)))
            query = FeatureQuery(features, core, 't')
            outcome = TimeOutcome(str(i), i * 10.0, i * 10.0 + 1, 'task', str(i), '0',
                                  duration_ms=(i + 1) * 37.0)
            return TrainingEvent(query, outcome)
        training = [event(i) for i in range(20)]
        kb = EdgeKappaKB.fit(training, {'max_optional_features': maximum,
                             'shared_child_weight': shared, 'learn_weights': learn})
        digest = hashlib.sha256()
        def record(value, digest=digest):
            digest.update(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
            digest.update(b'\n')
        record(kb.to_snapshot())
        for i in range(20, 70):
            item = event(i)
            pred, _ = kb.begin(item.query, str(i), i * 10.0,
                               task_id='task', call_id=str(i), clause_id='0')
            record(asdict(pred))
            kb.complete(str(i), item.outcome)
            record(asdict(kb.commit_before(i * 10.0 + 2)))
            record(kb.to_snapshot())
            if i % 10 == 0:
                kb = EdgeKappaKB.from_snapshot(kb.to_snapshot())
        results.append({'maximum': maximum, 'shared': shared, 'learn': learn,
                        'nodes': len(kb.nodes), 'edges': len(kb.edges), 'sha256': digest.hexdigest()})
args.out.write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
