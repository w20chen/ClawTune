import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {readSpec,convert} from '../import_fig.mjs';

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
