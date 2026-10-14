import gzip
import json
import re
import sys
from pathlib import Path

out = Path(__file__).resolve().parent
rows = json.loads(gzip.decompress((out / 'worst30-evidence.json.gz').read_bytes()))
lo, hi = map(int, sys.argv[1:3])
for row in rows[lo-1:hi]:
    raw = row['raw_data']
    output = str(raw.get('tool_result', raw.get('result', raw.get('output', ''))))
    lines = output.splitlines()
    interesting = [l for l in lines if re.search(r'passed|failed|errors?|Error|No module|not found|seconds|timed out|Timeout|Killed|tests? in|collected|Installing|Successfully|Exit code', l)]
    ev = []
    for c in row['clause_evidence']:
        ev.append({'argv': c['clause']['argv'], 'exact': c['exact'], 'n': c['count'],
            'p50_s': c['p50_ms']/1000, 'range_s': [c['min_ms']/1000, c['max_ms']/1000],
            'selected': c['selected_features'],
            'dropped': sorted(set(c['query_features'])-set(c['selected_features'])),
            'source_match': c['source_multiset_matches'],
            'largest_sources': [{'task':s['task'], 'argv':s['argv'], 'n':len(s['values_ms'])} for s in c['training_sources'][:3]]})
    actual_clauses = [{k: c.get(k) for k in ('argv','latency_ms','eligible_for_kb')}
                      for c in (raw.get('resource_observation') or {}).get('clauses', [])]
    print(json.dumps({'rank': row['rank'], 'id': row['id'], 'actual_s': row['actual_ms']/1000,
        'p50_s': row['p50_ms']/1000, 'error_s': row['absolute_error_ms']/1000,
        'command':row['command'], 'exit':raw.get('exit_code'), 'output': '\n'.join(interesting[-12:])[-2200:],
        'tail': output[-700:], 'actual_clauses':actual_clauses, 'evidence':ev}, ensure_ascii=False))
