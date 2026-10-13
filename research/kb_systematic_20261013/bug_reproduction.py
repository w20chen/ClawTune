"""Isolated before/after fixture validation; no network or training labels."""
import copy
import sys
import types
import zipfile
from command_parser import ParseContext
from command_parser.registry import RuleRegistry
from command_parser.generation import GeneratorConfig,RuleGenerator
from command_parser.evidence import record
from research.kb_systematic_20261013.study import HERE,write

old=types.ModuleType('command_parser.original_generation')
sys.modules[old.__name__]=old
archive = HERE/'source.zip'
with zipfile.ZipFile(archive if archive.exists() else 'source.zip') as z:
    source=z.read('services/sidecar/src/command_parser/generation.py').decode()
exec(compile(source,'archived-generation.py','exec'),old.__dict__)
rule,_=RuleRegistry().lookup('head',ParseContext())
rule['grammar']['options']=[]
rule['fixtures']=[{'argv':['python3','-m','unittest','tests.dialects.test_bigquery'],'status':'complete','target':['unittest']}]
class Help:
    def acquire(self,*args):return record('help','unittest','Usage: python -m unittest [tests...]','fixture')
result={}
for name,cls in [('before',old.RuleGenerator),('after',RuleGenerator)]:
    g=cls(RuleRegistry(),GeneratorConfig('https://example.invalid','test','test'),client=lambda body:copy.deepcopy(rule),help_runner=Help())
    try:
        g.generate('unittest',ParseContext(repo='r',runtime_id='r'),argv=['python3','-m','unittest','tests.dialects.test_bigquery'])
        result[name]={'success':True}
    except ValueError as e:result[name]={'success':False,'error':str(e)}
write(HERE/'results/bug-reproduction.json',result)
print(result)
assert result['before']['success'] is False and result['after']['success'] is True
