from research.kb_systematic_20261013.study import HERE,read
out=HERE/'results'
for r in read(out/'parameter-family-diagnostics.json')['rows']:
    if r['partition']=='train' and r['target']=='target:["pytest"]' and r['key'] in ('arg:L1/pytest:0','flag:L1/pytest:--quiet','opt:L1/pytest:--numprocesses','opt:L1/pytest:--tb'):
        print({**r,'values':r['values'][:4]})
inter=read(out/'parameter-interactions.json')['rows']
print('interactions',len(inter))
for r in sorted([r for r in inter if r['target']=='target:["pytest"]'],key=lambda r:-abs(r['interaction_log']))[:5]:print(r)
ci=read(out/'paired-uncertainty.json')
for key in ('baseline_lattice_vs_edge_native','baseline_lattice_vs_tool','baseline_native_vs_edge_uniform','shared_native_vs_pooled','llm_test_edge'):
    print(key,ci[key]['log_mae'],ci[key]['accuracy'])
print('policy',read(out/'llm/rule-policy.json'))
if (out/'interfaces/call-compatibility-summary.json').exists():
    d=read(out/'interfaces/call-compatibility-summary.json')
    for scope,v in d.items():
        print('legacy-call',scope,'common_n',v['common_n'],'legacy-full',v['all']['trie_legacy_median_composition'])
if (out/'interfaces/tool-legacy-p90-summary.json').exists():print('legacy-tool',read(out/'interfaces/tool-legacy-p90-summary.json'))
