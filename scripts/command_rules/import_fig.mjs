import ts from 'typescript';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

export const REVISION = 'aef52acff84c45edde61ae610cc2c964802b9a38';
const unsupported = Symbol('unsupported');
const array = value => Array.isArray(value) ? value : value === undefined ? [] : [value];

export function readSpec(filename, diagnostics = [], stack = new Set()) {
  filename = path.resolve(filename);
  if (stack.has(filename) || stack.size > 16) return unsupported;
  const active = new Set([...stack, filename]);
  const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true);
  const declarations = new Map();
  let exported;
  for (const stmt of source.statements) {
    if (ts.isVariableStatement(stmt)) for (const d of stmt.declarationList.declarations) declarations.set(d.name.getText(source), d.initializer);
    if (ts.isExportAssignment(stmt)) exported = stmt.expression;
    if (ts.isImportDeclaration(stmt) && stmt.moduleSpecifier.text.startsWith('.')) {
      const imported = path.resolve(path.dirname(filename), stmt.moduleSpecifier.text + '.ts');
      if (stmt.importClause?.name && fs.existsSync(imported)) declarations.set(stmt.importClause.name.text, {imported});
    }
  }
  function evaluate(node, visiting = new Set()) {
    if (!node) return unsupported;
    if (node.imported) return readSpec(node.imported, diagnostics, active);
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
    if (ts.isNumericLiteral(node)) return Number(node.text);
    if (node.kind === ts.SyntaxKind.TrueKeyword) return true;
    if (node.kind === ts.SyntaxKind.FalseKeyword) return false;
    if (node.kind === ts.SyntaxKind.NullKeyword) return null;
    if (ts.isAsExpression(node) || ts.isParenthesizedExpression(node) || ts.isSatisfiesExpression(node)) return evaluate(node.expression, visiting);
    if (ts.isIdentifier(node)) {
      if (visiting.has(node.text)) return unsupported;
      return evaluate(declarations.get(node.text), new Set([...visiting,node.text]));
    }
    if (ts.isArrayLiteralExpression(node)) {
      const result = [];
      for (const item of node.elements) {
        const value = evaluate(ts.isSpreadElement(item) ? item.expression : item, visiting);
        if (ts.isSpreadElement(item)) {
          if (!Array.isArray(value)) result.push(unsupported); else result.push(...value);
        } else result.push(value);
      }
      return result;
    }
    if (ts.isObjectLiteralExpression(node)) {
      const result = {};
      for (const prop of node.properties) {
        if (ts.isSpreadAssignment(prop)) {
          const value = evaluate(prop.expression, visiting);
          if (value === unsupported) result.__partial = true; else Object.assign(result,value);
        } else if (ts.isPropertyAssignment(prop)) result[prop.name.text ?? prop.name.getText(source)] = evaluate(prop.initializer, visiting);
        else if (ts.isShorthandPropertyAssignment(prop)) result[prop.name.text] = evaluate(prop.name, visiting);
      }
      return result;
    }
    return unsupported;
  }
  const value = evaluate(exported ?? declarations.get('completionSpec'));
  if (value === unsupported) diagnostics.push({file: filename, reason:'unresolved_default_export'});
  return value;
}

function arity(args) {
  const slots = array(args);
  if (slots.some(s => !s || typeof s !== 'object' || s.__partial ||
      ['isOptional','isVariadic'].some(key => s[key] !== undefined && typeof s[key] !== 'boolean'))) return null;
  return {min: slots.filter(s => !s.isOptional).length, max: slots.some(s => s.isVariadic) ? null : slots.length};
}

export function convert(spec, name, diagnostics = []) {
  function grammar(value, location) {
    const partial = [];
    const options = [];
    for (const o of array(value.options)) {
      if (!o || o === unsupported || o.__partial || o.name === unsupported) {partial.push(location+':options'); continue;}
      const names = array(o.name).filter(n=>typeof n==='string');
      const a = arity(o.args);
      if (!names.length || !a) {partial.push(location+':option-arity'); continue;}
      const canonical = [...names].sort((a,b)=>(b.startsWith('--')-a.startsWith('--')) || a.localeCompare(b))[0];
      options.push({names,canonical,...a,forms:['separate','equals','attached'],optional_value:a.max===null?'next':'attached',dash_values:'attached',
                    repeat:'ordered',independent:false,persistent:o.isPersistent===true});
    }
    const positions = [];
    for (const p of array(value.args)) {
      if (!arity(p)) {
        positions.push({min:0,max:null,role:'opaque_payload'});
        partial.push(location+':args');
        break;
      }
      positions.push({min: p.isOptional ? 0 : 1,max:p.isVariadic ? null : 1,role:'operand'});
    }
    const ambiguous = positions.findIndex((p,i)=>p.max===null && i<positions.length-1);
    if(ambiguous>=0) {
      positions.splice(ambiguous,positions.length-ambiguous,{min:0,max:null,role:'opaque_payload'});
      partial.push(location+':ambiguous-variadic-boundary');
    }
    const subs=[];
    for(const s of array(value.subcommands)) {
      if(!s || s===unsupported || s.name===unsupported) {partial.push(location+':subcommands'); continue;}
      const names=array(s.name).filter(n=>typeof n==='string');
      if(names.length) subs.push({names,canonical:names[0],grammar:grammar(s,location+'/'+names[0])});
    }
    for(const key of ['generateSpec','loadSpec','__partial']) if(value[key] !== undefined) partial.push(location+':'+key);
    diagnostics.push(...partial.map(reason=>({tool:name,reason})));
    return {options,positionals:positions,subcommands:subs,syntax:{clusters:false,intermixed:true,assignments:false},partial};
  }
  if(spec===unsupported) throw new Error('unresolved spec: '+name);
  return {format:'command-rule.v1',canonical:name,names:[name],scope:{kind:'global'},
          origin:{kind:'fig',source:`https://github.com/withfig/autocomplete/blob/${REVISION}/src/${name}.ts`,revision:REVISION},
          grammar:grammar(spec,name)};
}

export function importRules(source, destination, names, overrides = path.join(path.dirname(fileURLToPath(import.meta.url)),'overrides')) {
  fs.mkdirSync(destination,{recursive:true});
  const diagnostics=[], manifest={source_revision:REVISION,rules:{},diagnostics};
  for(const name of names) {
    const input=path.join(source,'src',name+'.ts');
    const patch=path.join(overrides,name+'.json');
    const change=fs.existsSync(patch)?JSON.parse(fs.readFileSync(patch,'utf8')):null;
    // A curated full rule also handles specs whose default export is dynamic.
    // Never evaluate the source factory just to replace its result afterwards.
    let rule=change?.full;
    if(!rule && fs.existsSync(input)) rule=convert(readSpec(input,diagnostics),name,diagnostics);
    if(change) {
      if(!change.full) {
        if(!rule) throw new Error('override needs source: '+name);
        if(change.names) rule.names=change.names;
        Object.assign(rule.grammar,change.grammar ?? {});
        for(const [spelling, values] of Object.entries(change.options ?? {})) {
          const option=rule.grammar.options.find(o=>o.names.includes(spelling));
          if(!option) throw new Error('unmatched override '+name+' '+spelling);
          Object.assign(option,values);
        }
        for(const option of change.add_options ?? []) {
          if(rule.grammar.options.some(o=>o.names.some(n=>option.names.includes(n)))) throw new Error('duplicate added option '+option.canonical);
          rule.grammar.options.push(option);
        }
        rule.grammar.options=rule.grammar.options.filter(o=>!(change.remove_options??[]).some(n=>o.names.includes(n)));
        for(const [commandPath, patch] of Object.entries(change.subcommand_options ?? {})) {
          let grammar=rule.grammar;
          for(const name of commandPath.split('/')) {
            const sub=grammar.subcommands.find(s=>s.names.includes(name));
            if(!sub) throw new Error('unmatched subcommand override '+commandPath);
            grammar=sub.grammar;
          }
          Object.assign(grammar,patch.grammar ?? {});
          for(const option of patch.add ?? []) {
            if(grammar.options.some(o=>o.names.some(n=>option.names.includes(n)))) throw new Error('duplicate added option '+option.canonical);
            grammar.options.push(option);
          }
          for(const [spelling,values] of Object.entries(patch.update ?? {})) {
            const option=grammar.options.find(o=>o.names.includes(spelling));
            if(!option) throw new Error('unmatched nested option '+spelling);
            Object.assign(option,values);
          }
        }
      }
      rule.origin.evidence=[...(rule.origin.evidence??[]),...(change.evidence??[]),`overrides/${name}.json`];
    }
    if(!rule) {diagnostics.push({tool:name,reason:'missing_source'});continue;}
    const output=JSON.stringify(rule,null,2)+'\n';
    fs.writeFileSync(path.join(destination,name+'.json'),output);
    manifest.rules[name]=crypto.createHash('sha256').update(output).digest('hex');
  }
  fs.copyFileSync(path.join(source,'LICENSE'),path.join(destination,'FIG-LICENSE'));
  fs.writeFileSync(path.join(destination,'manifest.json'),JSON.stringify(manifest,null,2)+'\n');
  return manifest;
}

if(process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  const [, , source, destination, ...names]=process.argv;
  if(!source || !destination || !names.length) throw new Error('usage: import_fig.mjs SOURCE OUTPUT TOOL...');
  const revision=execFileSync('git',['-C',source,'rev-parse','HEAD'],{encoding:'utf8'}).trim();
  if(revision!==REVISION) throw new Error('Fig checkout does not match pinned revision');
  if(execFileSync('git',['-C',source,'status','--porcelain','--','src','LICENSE'],{encoding:'utf8'}).trim()) throw new Error('Fig source has uncommitted changes');
  const result=importRules(source,destination,names);
  console.log(JSON.stringify({rules:Object.keys(result.rules).length,diagnostics:result.diagnostics.length}));
}
