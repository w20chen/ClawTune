from collections import Counter
from research.kb_systematic_20261013.study import HERE,read
d=read(HERE/'results/production-verification.json')['differences']
print(Counter(x['model'] for x in d))
for r in d[:15]:
    print(r['id'],r['model'],r['production']['p50'],r['study']['p50'],r['production']['unavailable_reason'],r['study']['unavailable_reason'],repr(r['study']['command'][:160]))
