import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {readSpec,convert,importRules} from '../import_fig.mjs';

test('static import resolves declarations and never executes callbacks',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'clawtune-fig-'));
  try {
    const filename=path.join(root,'example.ts');
    fs.writeFileSync(filename,`const opts=[{name:['-n','--count'],args:{name:'COUNT'}}];
      const completionSpec={name:'example',options:[...opts],generateSpec:()=>{throw new Error('must not run')}};
      export default completionSpec;`);
    const diagnostics=[];
    const result=convert(readSpec(filename),'example',diagnostics);
    assert.equal(result.grammar.options[0].canonical,'--count');
    assert.equal(result.grammar.options[0].min,1);
    assert.ok(diagnostics.some(row=>row.reason.includes('generateSpec')));
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

for (const expression of [
  "{name:'--x', ...getOption()}",
  "{name:'--x', args:{...getArgs()}}",
  "{name:'--x', args:{isOptional:getOptional()}}",
  "{name:'--x', args:{isVariadic:getVariadic()}}",
]) test('unresolved grammar fields stay unsupported: '+expression,()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'clawtune-fig-'));
  try {
    const filename=path.join(root,'example.ts');
    fs.writeFileSync(filename,`export default {name:'example',options:[${expression}]};`);
    const result=convert(readSpec(filename),'example');
    assert.equal(result.grammar.options.length,0);
    assert.ok(result.grammar.partial.length);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('unknown positional arity cannot shift a later argument into its place',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'clawtune-fig-'));
  try {
    const filename=path.join(root,'example.ts');
    fs.writeFileSync(filename,`export default {name:'example',args:[getArgs(),{name:'file'}]};`);
    const result=convert(readSpec(filename),'example');
    assert.deepEqual(result.grammar.positionals,[{min:0,max:null,role:'opaque_payload'}]);
    assert.ok(result.grammar.partial.length);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('variadic option accepts separate values after its minimum',()=>{
  const result=convert({options:[{name:'--files',args:{isVariadic:true}}]},'example');
  assert.equal(result.grammar.options[0].min,1);
  assert.equal(result.grammar.options[0].max,null);
  assert.equal(result.grammar.options[0].optional_value,'next');
});

test('full override bypasses a dynamic export without executing it',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'clawtune-fig-'));
  try {
    const source=path.join(root,'source'), overrides=path.join(root,'overrides'), output=path.join(root,'output');
    fs.mkdirSync(path.join(source,'src'),{recursive:true});
    fs.mkdirSync(overrides);
    fs.writeFileSync(path.join(source,'LICENSE'),'fixture license');
    fs.writeFileSync(path.join(source,'src','example.ts'),`export default (()=>{throw new Error('must not run')})();`);
    const full=convert({options:[]},'example');
    fs.writeFileSync(path.join(overrides,'example.json'),JSON.stringify({full}));
    const manifest=importRules(source,output,['example'],overrides);
    assert.ok(manifest.rules.example);
    assert.equal(JSON.parse(fs.readFileSync(path.join(output,'example.json'))).canonical,'example');
    assert.deepEqual(manifest,importRules(source,output,['example'],overrides));
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('unknown argument expressions never become boolean options',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'clawtune-fig-'));
  try {
    const filename=path.join(root,'example.ts');
    fs.writeFileSync(filename,`export default {name:'example',options:[{name:'--x',args:getArgs()}]};`);
    const result=convert(readSpec(filename),'example');
    assert.equal(result.grammar.options.length,0);
    assert.ok(result.grammar.partial.length);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
