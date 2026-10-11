"""Read-only source audit and measured clause export; no shell execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'services/sidecar/src')]
from cold_start.flat_loader import read_task, recorded_clause_structure
from cold_start.manifest import sha256_file, task_identity
from tool_resource.runtime_kb import ClauseObservation, is_pipeline_dependent_consumer
from tool_resource.features import shell_bin_requires_exec_evidence
from tool_resource.sdk import _observations_from_call
from tool_time.resource_lattice import resource_values
from tool_time.edge_kappa_adapter import shell_query
from clawtune_sidecar.predictors.tool_resource import _completed_call_from_tool_span
from clawtune_sidecar.monitoring.environment_memory import memory_labels
from offline.runner import _safe_recorded_clause


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False)+'\n', encoding='utf-8')


def memory_scope_reason(resources, execution):
    """An eligible flag cannot make a disjoint measured cgroup valid."""
    if resources.get('memory_measurement') != 'cgroup_v2_memory_current':
        return None
    measured = resources.get('memory_environment_id') or ''
    executed = execution.get('cgroup_path') or ''
    if measured.startswith('/') and executed.startswith('/'):
        measured, executed = PurePosixPath(measured), PurePosixPath(executed)
        if measured != executed and measured not in executed.parents:
            return 'execution_outside_measured_memory_cgroup'
    return None


def prepare(flat: Path, output: Path):
    if output.exists() and any(output.iterdir()):
        raise ValueError('output must be empty; historical results are never overwritten')
    output.mkdir(parents=True, exist_ok=True)
    records, manifests, exclusions, files_audit, calls_audit = [], [], [], [], []
    memory_quarantine = []
    seen = set()

    def add(obs, meta, identity, command, call_id, line=None):
        if obs.in_loop or obs.in_subst or is_pipeline_dependent_consumer(obs):
            return 'unsupported_structure'
        if not shell_bin_requires_exec_evidence(obs.bin, obs.argv[0]):
            return 'not_exec_producing'
        query = shell_query(__import__('shlex').join(obs.argv), repo=obs.repo)
        values = resource_values(obs)
        if obs.latency_ms is not None:
            values['duration_ms'] = obs.latency_ms
        if not values:
            return 'no_valid_target'
        record = dict(meta, id=identity, call_id=call_id, source_line=line,
                      command=command, observation=asdict(obs), values=values,
                      tool=query.tool, features=sorted(query.features), core=sorted(query.core_features),
                      scope='clause')
        records.append(record)
        return 'accepted'

    for i, path in enumerate(sorted(flat.glob('*.trace.jsonl'))):
        with path.open(encoding='utf-8-sig') as f:
            metadata = json.loads(next(f))
        repo, task = task_identity(path.name)
        digest = sha256_file(path)
        run = metadata['runtime_dir'].split('/')[-4]
        proof = metadata.get('runtime_proof') or {}
        meta = dict(source=str(path.resolve()), sha256=digest, benchmark='swe-rebench', repo=repo.lower(),
                    task=task.lower(), run=run, stratum='v5:'+run,
                    hardware={'tool_arch':proof.get('tool_container_arch'), 'host_arch':None,
                              'cpu_quota':None, 'parallelism':None, 'emulation':None},
                    measurement_source='v5_qualified_clause', rss_input_unit='MiB',
                    rss_unit_evidence='existing bootstrap manifest; historical producer not pinned',
                    base_commit=None)
        manifests.append(meta)
        if digest in seen:
            exclusions.append(dict(source=str(path), reason='duplicate_file_hash')); continue
        seen.add(digest)
        loaded = read_task(path, repo=repo.lower(), task_id=task, rss_unit='MiB',
                           recorded_clauses_only=True, preserve_timestamps=True)
        counts = Counter(loaded.counts)
        for action in loaded.actions:
            for j, obs in enumerate(action.clauses):
                counts[add(obs, meta, digest+':'+action.action_id+':'+str(j), action.command,
                           action.action_id)] += 1
        for call, values in zip(loaded.calls, loaded.call_actuals):
            calls_audit.append(dict(stratum=meta['stratum'], repo=meta['repo'], task=meta['task'],
                                   censored=call.censored, targets=sorted(values) if not call.censored else []))
        files_audit.append(dict(source=meta['source'], stratum=meta['stratum'], counts=dict(counts)))
        if i % 100 == 0:
            print('v5', i, 'records', len(records), flush=True)

    # Download manifest is the authority for task identity (v6 headers omit it).
    batch = ROOT / '.runtime/downloads/batch-20260913-151054-9d96f2'
    entries = json.loads((batch/'manifest.json').read_text(encoding='utf-8'))['files']
    sources = [(batch/e['path'], e['task_id'], 'swe-rebench' if e['stage']=='swe' else 'terminal-bench',
                'v6:batch-20260913:'+e['stage'], e['sha256'], {}) for e in entries]
    recent = ROOT / '.runtime/downloads/terminal-bench-20260921-075311-c03dcdf5'
    run = json.loads((recent/'run.json').read_text(encoding='utf-8'))
    for result in run['results']:
        candidates = list((recent/'traces').glob('*/dataset-task.json'))
        for side in candidates:
            task = json.loads(side.read_text(encoding='utf-8'))
            if task.get('task_id', task.get('instance_id')) == result['task_id']:
                p = side.parent/'trace.jsonl'
                if p.exists():
                    sources.append((p,result['task_id'],'terminal-bench','v6:terminal-20260921',None,
                                    {'parallelism':run['parallelism'], 'platform':run['environment']['platform']}))
                break
    # Additional real run manifests, including failed runs: completed calls remain
    # eligible, while each run keeps its own hardware/concurrency stratum.
    task_digests={}
    for relative in ('.runtime/trace-audit', '.runtime/benchmark-validation',
                     '.runtime/experiment-30x2', '.runtime/tool-profile-study'):
        root=ROOT/relative
        candidates=sorted(set(root.rglob('run.json')) | set(root.rglob('report.json')))
        run_seen=set()
        for manifest_path in candidates:
            run=json.loads(manifest_path.read_text(encoding='utf-8'))
            if run.get('schema')!='clawtune.benchmark-run.v1' or 'results' not in run:continue
            if manifest_path.parent in run_seen:continue
            run_seen.add(manifest_path.parent)
            for result in run['results']:
                for remote in result.get('trace_files') or []:
                    if '/traces/' not in remote:continue
                    if Path(remote).name=='observation-issues.jsonl':continue
                    suffix=remote.split('/traces/',1)[1]
                    task_digests[suffix.split('/')[0]]=result['task_id']
                    local=manifest_path.parent/'traces'/suffix
                    if not local.exists():local=manifest_path.parent/'sidecar'/Path(remote).name
                    if not local.exists() and '__clawtune-' in Path(remote).name:
                        local=manifest_path.parent/'sidecar'/('clawtune-'+Path(remote).name.split('__clawtune-',1)[1])
                    if not local.exists():
                        exclusions.append(dict(source=remote,reason='trace_not_in_local_download',manifest=str(manifest_path)))
                        continue
                    sources.append((local,result['task_id'],run['benchmark'],
                        'v6:'+relative.split('/')[-1]+':'+manifest_path.parent.name,None,
                        {'parallelism':run.get('parallelism'),'platform':run.get('environment',{}).get('platform'),
                         'image':result.get('image'),'run_manifest':str(manifest_path),
                         'run_manifest_sha256':sha256_file(manifest_path)}))
    for path in sorted((ROOT/'.runtime/trace-audits').rglob('*.trace.jsonl')):
        task=task_digests.get(path.name.split('.')[0])
        if task is None:
            exclusions.append(dict(source=str(path),reason='unresolved_task_identity'));continue
        sources.append((path,task,'swe-rebench','v6:trace-audits:'+path.parent.name,None,
                        {'parallelism':None,'task_identity_source':'task-directory digest in benchmark run manifests'}))
    for path, task, benchmark, stratum, expected, hardware in sources:
        digest = sha256_file(path)
        if expected and expected != digest:
            raise ValueError('source manifest hash mismatch: '+str(path))
        if digest in seen:
            exclusions.append(dict(source=str(path),reason='duplicate_file_hash')); continue
        seen.add(digest)
        repo = task_identity(task)[0].lower() if benchmark in {'swe-rebench','swe-bench-verified'} else None
        meta = dict(source=str(path.resolve()),sha256=digest,benchmark=benchmark,repo=repo,
                    task=task.lower(),run=stratum,stratum=stratum,hardware=hardware,
                    measurement_source='v6_qualified_clause',rss_input_unit='MB',base_commit=None)
        manifests.append(meta)
        starts, counts, artifacts = {}, Counter(), {}
        with path.open(encoding='utf-8-sig') as f:
            for line in f:
                if '"trace_event"' not in line: continue
                event=json.loads(line)
                if event.get('record_type')!='trace_event': continue
                artifact=event.get('artifact') or {}
                for call in artifact.get('calls') or []:
                    artifacts[(event.get('execution_id'),call.get('tool_call_id'))]=(call,artifact.get('quota_cores'))
        with path.open(encoding='utf-8-sig') as f:
            for line_no, line in enumerate(f,1):
                row=json.loads(line)
                if row.get('kind')!='tool' or row.get('record_type') not in {'span_start','span_end'}: continue
                sid=row['span_id']
                if row['record_type']=='span_start': starts[sid]=row;continue
                if row['record_type']!='span_end': continue
                counts['tool_ends']+=1
                span_repo=row.get('repo') or repo or 'unknown'
                if repo and span_repo.split(':',1)[-1].lower()!=repo:
                    raise ValueError('source task/repository identity mismatch')
                completed=_completed_call_from_tool_span(starts.get(sid),row,repo=span_repo)
                resources=row.get('resources') or {}
                if (resources.get('pmu') or {}).get('architecture'):
                    meta['hardware']['host_arch'] = resources['pmu']['architecture']
                counts['memory_reason:'+str(resources.get('memory_unavailable_reason','absent'))]+=1
                labels = memory_labels(resources)
                scope_reason = memory_scope_reason(resources, row.get('execution') or {})
                if scope_reason:
                    counts['memory_scope_rejected_tool'] += int(bool(labels))
                    memory_quarantine.append(dict(source=meta['source'],sha256=digest,source_line=line_no,
                        span_id=sid,repo=repo,reason=scope_reason,
                        measured_cgroup=resources.get('memory_environment_id'),
                        execution_cgroup=(row.get('execution') or {}).get('cgroup_path'),
                        tool_labels=labels,clause_labels=resources.get('memory_clause_observations') or []))
                    labels = {}
                counts['tool_valid_extra_memory'] += int('memory_extra_peak_bytes' in labels and completed is not None and not completed.censored)
                if completed is None or completed.censored:
                    counts['missing_or_censored_completion']+=1;continue
                telem=(row.get('execution') or {}).get('tool_resource') or {}
                call=telem.get('call_telemetry') or {}
                raw=artifacts.get(((row.get('execution') or {}).get('execution_id'),call.get('tool_call_id')))
                call_meta=dict(meta, hardware=dict(meta['hardware']))
                if raw is not None:
                    if raw[0].get('command') != call.get('command'): raise ValueError('artifact command mismatch')
                    call=raw[0]
                    call_meta['hardware']['cpu_quota']=raw[1]
                    images=[image for clause in call.get('clauses') or [] for image in
                            (clause.get('provenance') or {}).get('per_image_diagnostics',[])]
                    interpreters=[str(image.get('bprm_interp','')) for image in images]
                    call_meta['hardware']['emulation']='qemu' if any('qemu' in x for x in interpreters) else 'unverified'
                counts['raw_clauses']+=len(call.get('clauses') or [])
                if call.get('eligible_for_kb') is True and call.get('telemetry_quality')=='ok':
                    try:
                        clauses=recorded_clause_structure(call.get('command'),call.get('clauses'),recorded_only=True)
                    except ValueError:
                        # Existing offline guard proves only a literal single
                        # argv, with no shell operators/expansions. Never infer
                        # compound structure or transfer tool labels to clauses.
                        safe=_safe_recorded_clause(call.get('command'),tuple(call.get('clauses') or []))
                        if safe:
                            clauses=tuple(dict(c,in_loop=False,in_pipe=False,in_subst=False,pipeline_position=-1) for c in safe)
                            counts['proven_plain_single_clause_structure']+=1
                        else:
                            counts['missing_recorded_clause_structure']+=1;clauses=[]
                    for j, obs in enumerate(_observations_from_call(span_repo,dict(call,clauses=list(clauses)),require_timestamps=True,enrich_structure=False)):
                        counts[add(replace(obs,repo=repo or span_repo),call_meta,digest+':'+sid+':'+str(j),call.get('command'),sid,line_no)]+=1
                else: counts['ineligible_call_telemetry']+=1
                for j, value in enumerate(resources.get('memory_clause_observations') or []):
                    if value.get('repo') != span_repo or not memory_labels(value):continue
                    if scope_reason or memory_scope_reason(value, row.get('execution') or {}):
                        counts['memory_scope_rejected_clause'] += 1
                        continue
                    obs=ClauseObservation(**dict(value,argv=tuple(value['argv']),repo=repo or span_repo))
                    # Memory-only row is a separate target view, not another duration sample.
                    assert obs.latency_ms is None and obs.cpu_ns_cumulative is None
                    counts[add(obs,call_meta,digest+':'+sid+':memory:'+str(j),call.get('command'),sid,line_no)]+=1
        files_audit.append(dict(source=meta['source'],stratum=stratum,counts=dict(counts)))
    summary={}
    for stratum in sorted({m['stratum'] for m in manifests}):
        rows=[r for r in records if r['stratum']==stratum]
        counts=Counter()
        for r in rows: counts.update(r['values'].keys())
        summary[stratum]=dict(files=sum(m['stratum']==stratum for m in manifests),
            observations=len(rows), repositories=len({r['repo'] for r in rows if r['repo']}),
            tasks=len({r['task'] for r in rows}),targets=dict(counts),
            repository_identity_available=all(r['repo'] is not None for r in rows))
    with (output/'observations.jsonl').open('w',encoding='utf-8') as f:
        for r in records:f.write(json.dumps(r,sort_keys=True,allow_nan=False)+'\n')
    write(output/'source_manifest.json',manifests)
    write(output/'memory_scope_quarantine.json',memory_quarantine)
    write(output/'data_quality.json',dict(strata=summary,files=files_audit,excluded=exclusions,
                                       tool_level_audit=dict(Counter(t for c in calls_audit for t in c['targets']))))
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--flat',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();prepare(a.flat,a.output)
