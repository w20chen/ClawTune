"""Real DeepSeek requests; credentials are read in memory and never archived."""
import argparse
import json
import os
from pathlib import Path
import time
import httpx
from research.kb_systematic_20261013.study import write, read, fingerprint, HERE

class Provider:
    def __init__(self,out):
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
        keypath=Path(os.environ.get('CLAWTUNE_STUDY_KEY_FILE','/home/weitianc/ClawTune/swe_rebench/llm_api_key.txt'))
        self.key=keypath.read_text().strip()
        self.base='https://api.deepseek.com'
        self.model=os.environ.get('CLAWTUNE_STUDY_MODEL','deepseek-chat')
        self.records=[]
    def models(self):
        with httpx.Client(timeout=45,trust_env=False) as c:
            r=c.get(self.base+'/models',headers={'Authorization':'Bearer '+self.key})
            data=r.json()
        ids=[x['id'] for x in data.get('data',[])]
        write(self.out/'provider-check.json',{'http_status':r.status_code,'models':ids,'key_present':bool(self.key),
                                            'error_type':data.get('error',{}).get('type')})
        if r.status_code!=200:raise RuntimeError('provider_models_http_'+str(r.status_code))
        if self.model not in ids:
            self.model=next((m for m in ('deepseek-flash','deepseek-v4-flash') if m in ids),ids[0])
        return ids
    def __call__(self,body):
        index=len(list(self.out.glob('request-*.json')))
        body=dict(body,model=self.model,thinking={'type':'disabled'})
        write(self.out/f'request-{index:03d}.json',body)
        record={'index':index,'requested_model':self.model,'prompt_sha256':fingerprint(body),'start_unix':time.time()}
        start=time.monotonic()
        try:
            with httpx.Client(timeout=180,trust_env=False) as client:
                response=client.post(self.base+'/v1/chat/completions',headers={'Authorization':'Bearer '+self.key},json=body)
            record['http_status']=response.status_code
            if response.status_code!=200:
                record['error_type']=response.json().get('error',{}).get('type')
                raise RuntimeError('provider_http_'+str(response.status_code))
            data=response.json();record.update(model=data.get('model'),usage=data.get('usage'),finish_reason=data['choices'][0].get('finish_reason'))
            # Response contains no request authorization header.
            write(self.out/f'response-{index:03d}.json',data)
            return data['choices'][0]['message']['content']
        finally:
            record['elapsed_s']=time.monotonic()-start
            write(self.out/f'usage-{index:03d}.json',record)

if __name__=='__main__':
    p=Provider(HERE/'results/llm')
    try:
        print(json.dumps({'models':p.models(),'selected':p.model}),flush=True)
        print(p({'messages':[{'role':'user','content':'Return JSON {"ready":true}.'}], 'response_format':{'type':'json_object'},'temperature':0,'max_tokens':64}),flush=True)
    except Exception as e:
        write(HERE/'results/llm/blocked.json',{'exception':type(e).__name__,'reason':str(e)[:200]})
        raise
