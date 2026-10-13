"""Print metadata/development diagnostics only before the selection lock."""
from collections import Counter
from research.kb_systematic_20261013.study import HERE,read
p=HERE/'results'
if (p/'llm/rule-generation-summary.json').exists():
    d=read(p/'llm/rule-generation-summary.json')
    print('rule failures',[(x['entry'],x['jobs']) for x in d['failed']])
if (p/'inventory.json').exists():
    d=read(p/'inventory.json')
    print({k:d[k] for k in ['files','valid_calls','observations','repositories','parser_counts','tools','counts']})
if (p/'event-inventory.json').exists():print(read(p/'event-inventory.json'))
if (p/'observations.json.gz').exists():
    rows=read(p/'observations.json.gz')
    print('train clause tools',Counter(r['observation']['bin'] for r in rows if r['partition']=='train'))
if (p/'calls.json.gz').exists():
    from command_parser import default_service,ParseContext
    unknown=Counter();examples={}
    for c in read(p/'calls.json.gz'):
        if c['partition']!='train':continue
        for cl in c['clauses']:
            parsed=default_service().parser.parse(cl.get('syntax_argv',cl['argv']),ParseContext(repo=c['repo']))
            for d in parsed.diagnostics:
                if d.startswith('missing_rule:'):
                    entry=d.split(':',1)[1];unknown[entry]+=1;examples.setdefault(entry,cl['argv'])
    print('unknown',unknown)
    print('examples',[(k,examples[k]) for k,v in unknown.most_common(15)])
