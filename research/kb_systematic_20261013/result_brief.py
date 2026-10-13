from research.kb_systematic_20261013.study import HERE,read
p=HERE/'results'
for filename in ('baseline-metrics.json','optimization-test.json','transfer-test.json','llm/evaluation.json','calibration-summary.json','production-verification.json'):
    if not (p/filename).exists():continue
    d=read(p/filename);print(filename)
    if filename=='baseline-metrics.json':
        for scope in ('all','original_test'):
            print(scope,'common_n',d[scope]['common_n'])
            for n,m in d[scope]['full'].items():print(n,{k:m[k] for k in ('predicted','coverage','accuracy','macro_recall','log_mae')})
    elif filename=='optimization-test.json':
        print('selected',d['selected'],'common',d['common_n'])
        for n,m in d['common'].items():print(n,{k:m[k] for k in ('coverage','accuracy','macro_recall','log_mae')})
    elif filename=='transfer-test.json':
        print('selected',d['selected'])
        for n,m in d['test'].items():print(n,{k:m[k] for k in ('coverage','accuracy','macro_recall','log_mae')})
    elif filename=='production-verification.json':print({k:v for k,v in d.items() if k!='differences'},'differences',len(d['differences']))
    elif filename=='calibration-summary.json':
        for r in d:print(r['name'],r['feedback'],{k:r['metrics'][k] for k in ('coverage','accuracy','log_mae')})
    else:
        for part,v in d.items():
            print(part,v['changed_calls'])
            for n,m in v['all'].items():print(n,{k:m[k] for k in ('coverage','accuracy','log_mae')})
