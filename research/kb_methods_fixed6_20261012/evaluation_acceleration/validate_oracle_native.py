import itertools
import json
from pathlib import Path
import random
import time
from oracle_native import compose, reference
from clawtune_sidecar.prediction_config import load_bucket_edges

rng=random.Random(429)
start=time.monotonic()
count=0
for case in range(1000):
    n=rng.randrange(2,12)
    rows=[{'duration_ms':{'values':tuple(rng.choice((0.,1.,1e-8,1e7,1e14))*rng.random()
        for _ in range(rng.choice((1,2,3,4,7,8,23,64,65,103,2049))))}} for _ in range(n)]
    # Include sequential and parallel groupings using the production grouper.
    clauses=[{'argv':['test'], 'in_pipe': case%2==1,
              'pipeline_position': i if case%2 else -1,
              'pipeline_id': 'p' if case%2 else None} for i in range(n)]
    edges=load_bucket_edges((100.,500.,2000.,10000.))
    expected=reference('lattice',rows,edges,clauses=clauses).targets['duration_ms']
    actual=compose('lattice',rows,edges,clauses=clauses).targets['duration_ms']
    assert actual.status==expected.status
    assert actual.p50==expected.p50,(case,actual.p50,expected.p50)
    count+=1
result=dict(bit_exact_cases=count,elapsed_seconds=time.monotonic()-start,
    definition='same CPython Random(0) index draws, 2048 samples, grouping, compensated sum, median; no tolerance used')
Path('.runtime/oracle-native-validation.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
