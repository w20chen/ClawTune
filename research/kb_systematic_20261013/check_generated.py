import json
from command_parser.registry import RuleRegistry
from command_parser.parser import CommandParser
from command_parser.models import ParseContext
from research.kb_systematic_20261013.study import HERE,read,write
out=HERE/'results';diagnostics=[]
for p in sorted((out/'llm/rules').glob('response-*.json')):
    r=read(p);content=r['choices'][0]['message']['content']
    try:rule=json.loads(content)
    except (ValueError,TypeError):continue
    rule['scope']={'kind':'runtime','repo':'swe-rebench:tobymao/sqlglot','runtime_id':'study-source','entry':rule['names'][0]}
    context=ParseContext(repo='swe-rebench:tobymao/sqlglot',runtime_id='study-source')
    try:
        reg=RuleRegistry(bundled=False);reg.add(rule,persist=False)
        for fx in rule.get('fixtures',[]):
            parsed=CommandParser(reg).parse(fx['argv'],context)
            if parsed.status!=fx['status'] or parsed.target!=fx['target']:
                diagnostics.append({'response':p.name,'entry':rule['names'],'fixture':fx,'parsed':parsed.to_dict()})
    except Exception as e:diagnostics.append({'response':p.name,'error':str(e)[:200]})
write(out/'llm/fixture-failures.json',diagnostics)
for d in diagnostics[:15]:
    print(json.dumps({k:v for k,v in d.items() if k!='parsed'}|({'actual_target':d['parsed']['target'],'actual_status':d['parsed']['status'],'diagnostics':d['parsed']['diagnostics']} if 'parsed' in d else {})))
