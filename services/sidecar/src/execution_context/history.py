"""Pre-call execution history shared by trace evaluation and the sidecar.

No probes are executed. Completed outcomes are visible only to later calls.
"""
from __future__ import annotations

import ast
import json
import math
import posixpath
import re
import shlex

RELEVANT_ENV = frozenset(('PATH', 'PYTHONPATH', 'VIRTUAL_ENV', 'CONDA_PREFIX',
    'PYTEST_ADDOPTS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'SQLGLOTRS_TOKENIZER'))
TTL_SECONDS = 600.0


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def params(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def command_input(args):
    args = params(args)
    return next((args[k] for k in ('command', 'cmd', 'input', 'code')
                 if isinstance(args.get(k), str)), None)


def declared_context(args):
    """Parse one literal command and at most one literal, conditional cd.

    The working directory is the call argument, not collector metadata or a
    measured process cwd. Literal entry paths are not resolved interpreters.
    """
    args = params(args)
    command = command_input(args)
    if not command or any(c in command for c in ('$','`','\n')):
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
        lex.whitespace_split = True
        lex.commenters = ''
        words = list(lex)
    except ValueError:
        return None
    cwd = next((args[k] for k in ('working_dir','working_directory','cwd','workdir')
                if isinstance(args.get(k),str) and args[k]), None)
    if cwd is not None and any(c in cwd for c in '$`~*?[]'):
        cwd = None
    if len(words) > 3 and words[0] == 'cd' and words[2] == '&&':
        dest, words = words[1], words[3:]
        if dest in ('-', '') or any(c in dest for c in '$`~*?[]'):
            return None
        cwd = posixpath.normpath(dest if dest.startswith('/') else posixpath.join(cwd,dest)) if (dest.startswith('/') or cwd) else None
    if not words or any(any(c in w for c in ';&|<>()*?[]') for w in words):
        return None
    env = {k:v for k,v in params(args.get('env')).items() if k in RELEVANT_ENV and isinstance(v,str)}
    if words[0] == 'env':
        words = words[1:]
    while words and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', words[0]):
        name,value=words.pop(0).split('=',1)
        if name in RELEVANT_ENV:
            env[name]=value
    if not words or words[0].startswith('-'):
        return None
    entry=words[0]
    name=posixpath.basename(entry)
    is_python=bool(re.fullmatch(r'python(?:[23](?:\.\d+)?)?',name))
    is_pytest=bool(re.fullmatch(r'pytest(?:-[23](?:\.\d+)?)?',name))
    module=words[2] if is_python and len(words)>2 and words[1]=='-m' else None
    family='pytest' if is_pytest or module=='pytest' else 'python' if is_python else 'other'
    is_test=family=='pytest' and not any(v in words[1:] for v in ('--version','--help','-h','--collect-only','--co'))
    return {'argv':words,'entry':entry,'cwd':cwd,'env':dict(sorted(env.items())),
            'family':family,'is_test':is_test,'module':module}



def pytest_observation(context, output):
    """Outcome evidence; caller may publish it only after the call finishes."""
    if context is None or context['family']!='pytest' or not isinstance(output,str):
        return None
    if re.search(r'\b\d+ (?:passed|failed|xfailed|xpassed)\b',output) and re.search(r'\bin \d+(?:\.\d+)?s\b',output):
        return {'outcome':'tests_executed','reason':None}
    missing=re.search(r"(?:ModuleNotFoundError: |ImportError: )?No module named ['\"]?([A-Za-z_][A-Za-z_0-9.]*)",output)
    startup=bool(re.search(r'ConftestImportFailure|ImportError while loading conftest|(?:python[^\n]*: )?No module named pytest\b|pytest: (?:not found|command not found)',output))
    collection=bool(re.search(r'ERROR collecting|errors? during collection',output))
    if startup or collection:
        if missing:
            reason='missing_module:'+missing.group(1)
        else:
            name=re.search(r"cannot import name ['\"]([^'\"]+)['\"]",output)
            reason='import_name:'+name.group(1) if name else 'import_or_collection_error'
        return {'outcome':'startup_failure' if startup else 'collection_failure','reason':reason}
    return None


def literal_parts(command):
    """Split a small literal shell subset, preserving quoted Python code.

    Reject expansion, pipelines, substitutions, redirection, background work,
    newlines and control structures. Never split punctuation inside quotes.
    """
    if not command:
        return None
    parts, operators = [], []
    quote = None
    start = i = 0
    while i < len(command):
        char = command[i]
        if char in '\n\r' or (char in '$`' and quote != "'"):
            return None
        if char == '\\' and quote != "'":
            return None  # escaped shell syntax deliberately outside this subset
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif command.startswith('&&', i) or char == ';':
            parts.append(command[start:i].strip())
            operator = '&&' if char == '&' else ';'
            operators.append(operator)
            i += len(operator)
            start = i
            continue
        elif char in '|&<>()*?[]#{}':
            return None
        i += 1
    parts.append(command[start:].strip())
    if quote or not all(parts):
        return None
    return parts, operators


def contexts(args):
    args = params(args)
    split = literal_parts(command_input(args))
    if split is None:
        return None
    parts, operators = split
    cwd = next((args[k] for k in ('working_dir', 'working_directory', 'cwd', 'workdir')
                if isinstance(args.get(k), str) and args[k]), None)
    if cwd and any(c in cwd for c in '$`~*?[]'):
        cwd = None
    result = []
    for index, part in enumerate(parts):
        try:
            words = shlex.split(part)
        except ValueError:
            return None
        if not words:
            return None
        if words[0] == 'cd':
            if index != 0 or len(words) != 2 or not operators or operators[0] != '&&':
                return None
            dest = words[1]
            if not dest or dest == '-' or any(c in dest for c in '$`~*?[]'):
                return None
            cwd = posixpath.normpath(dest if dest.startswith('/') else posixpath.join(cwd, dest)) if (dest.startswith('/') or cwd) else None
            continue
        env = {k: v for k, v in params(args.get('env')).items() if k in RELEVANT_ENV and isinstance(v, str)}
        if words[0] == 'env':
            words = words[1:]
        while words and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', words[0]):
            name, value = words.pop(0).split('=', 1)
            if name in RELEVANT_ENV:
                env[name] = value
        if not words or words[0].startswith(('-', '~')):
            return None
        name = posixpath.basename(words[0])
        python = bool(re.fullmatch(r'python(?:[23](?:\.\d+)?)?', name))
        module = words[2] if python and len(words) > 2 and words[1] == '-m' else None
        family = 'pytest' if re.fullmatch(r'pytest(?:-[23](?:\.\d+)?)?', name) or module == 'pytest' else 'unittest' if module == 'unittest' else 'python' if python else 'other'
        result.append({'argv': words, 'entry': words[0], 'cwd': cwd, 'env': dict(sorted(env.items())),
                       'family': family, 'module': module})
    return result or None


def probe(context):
    if context['family'] != 'python':
        return None
    argv = context['argv'][1:]
    if argv in (['--version'], ['-V']):
        return 'version'
    if len(argv) != 2 or argv[0] != '-c':
        return None
    try:
        body = ast.parse(argv[1]).body
    except (SyntaxError, ValueError):
        return None
    imports = [node for node in body if isinstance(node, (ast.Import, ast.ImportFrom))]
    if not imports:
        return None
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name) and node.value.func.id == 'print'
                and not node.value.keywords
                and all(isinstance(arg, (ast.Name, ast.Attribute, ast.Constant)) for arg in node.value.args)
                and not any(isinstance(child, (ast.Call, ast.Subscript)) for arg in node.value.args for child in ast.walk(arg))):
            return None
    return 'import'


def is_test(context):
    return context['family'] in ('pytest', 'unittest') and not any(
        arg in ('--version', '--help', '-h', '--collect-only', '--co') for arg in context['argv'][1:])


def quiet_diagnostic(context):
    argv = context['argv']
    return (probe(context) == 'version' or argv == ['pwd'] or argv == ['true']
            or (posixpath.basename(argv[0]) == 'which' and len(argv) == 2 and not argv[1].startswith('-')))


def mutates(tool, parsed):
    if tool not in ('exec', 'terminal_exec'):
        return tool not in ('read_file', 'list_dir', 'message', 'web_search', 'web_fetch')
    if not parsed:
        return True
    for context in parsed:
        if is_test(context) or quiet_diagnostic(context):
            continue
        argv = context['argv']
        name = posixpath.basename(argv[0])
        if name in ('ls', 'cat', 'head', 'tail', 'grep', 'rg', 'file', 'wc', 'nproc'):
            continue
        if name == 'git' and len(argv) > 1 and argv[1] in ('status', 'diff', 'log', 'show', 'rev-parse', 'ls-files'):
            continue
        return True
    return False


def identity(context):
    return json.dumps([context[k] for k in ('entry', 'cwd', 'env')], sort_keys=True)


def observations(parsed, output):
    """Only unambiguous producers; returned facts become visible on completion."""
    if not parsed or not isinstance(output, str):
        return []
    facts = []
    # A diagnostic bundle can contain one version producer. Import scripts may
    # mutate state, so a version preceding one is intentionally not retained.
    versions = [c for c in parsed if probe(c) == 'version']
    matches = re.findall(r'(?m)^Python (\d+\.\d+\.\d+)\s*$', output)
    if len(versions) == len(matches) == 1 and all(quiet_diagnostic(c) for c in parsed):
        facts.append((versions[0], 'python_version', matches[0], None))
    # Only one non-quiet producer, and it must be last. There is no inference
    # from mixed pip/build/test output or from a command that might be skipped.
    producers = [c for c in parsed if not quiet_diagnostic(c)]
    if len(producers) != 1 or producers[0] is not parsed[-1]:
        return facts
    context = producers[0]
    missing = re.search(r"(?:ModuleNotFoundError|ImportError): No module named ['\"]?([A-Za-z_][A-Za-z_0-9.]*)", output)
    if context['family'] == 'pytest':
        result = pytest_observation(context, output)
        if result:
            facts.append((context, 'runner_outcome', result['outcome'], result['reason']))
    elif context['family'] == 'unittest':
        ran = re.search(r'(?m)^Ran (\d+) tests? in [\d.]+s\s*$', output)
        if ran:
            # _FailedTest entries count as tests too: do not call these early
            # failures, and do not claim every counted test performed real work.
            outcome = 'tests_reported_with_import_errors' if 'Failed to import test module' in output else 'tests_executed'
            facts.append((context, 'runner_outcome', outcome, 'missing_module:' + missing.group(1) if missing else None))
        elif missing and ('Traceback (most recent call last):' in output or ': No module named unittest' in output):
            facts.append((context, 'runner_outcome', 'startup_failure', 'missing_module:' + missing.group(1)))
    elif probe(context) == 'import' and missing and 'Traceback (most recent call last):' in output:
        facts.append((context, 'import_failure', missing.group(1), None))
    return facts


class ExecutionEvidence:
    def __init__(self, scope, ttl=TTL_SECONDS):
        self.scope, self.ttl = scope, ttl
        self.generation = 0
        self.active = set()
        self.started, self.facts = {}, {}

    def begin(self, event):
        parsed = contexts(event['args']) if event['tool'] in ('exec', 'terminal_exec') else None
        snapshots = []
        for context in parsed or []:
            if context['family'] not in ('python', 'pytest', 'unittest'):
                continue
            visible = []
            if not self.active and event.get('history_clock_complete', True):
                for fact in self.facts.get(identity(context), {}).values():
                    if (fact['generation'] == self.generation and fact['available_at'] < event['start']
                            and event['start'] - fact['available_at'] <= self.ttl):
                        if fact['kind'] == 'runner_outcome' and fact['family'] != context['family']:
                            continue
                        visible.append({k: v for k, v in fact.items() if k not in ('generation', 'argv')}
                                       | {'same_argv': fact['argv'] == context['argv']})
            snapshots.append({'format': 'execution-evidence.v1', 'scope_id': self.scope,
                              'prediction_time': event['start'], 'argv': context['argv'],
                              'family': context['family'], 'declared': {k: context[k] for k in ('entry', 'cwd', 'env')},
                              'facts': sorted(visible, key=lambda f: (f['kind'], f['family'])),
                              'history_blocked': bool(self.active) or not event.get('history_clock_complete', True)})
        # Fix pre-call snapshots BEFORE a command can mutate its own environment.
        mutation = mutates(event['tool'], parsed)
        if mutation:
            self.generation += 1
            self.active.add(event['id'])
        self.started[event['id']] = parsed, mutation, self.generation
        return snapshots

    def complete(self, event):
        parsed, mutation, generation = self.started.pop(event['id'])
        stable = generation == self.generation and not (self.active - {event['id']})
        if mutation:
            self.active.discard(event['id'])
            self.generation += 1
        if not stable:
            return
        # Unclassified same-family evidence supersedes earlier runner outcomes.
        for context in parsed or []:
            if is_test(context):
                self.facts.get(identity(context), {}).pop(('runner_outcome', context['family']), None)
        for context, kind, value, reason in observations(parsed, event.get('output')):
            family = context['family']
            self.facts.setdefault(identity(context), {})[(kind, family)] = {
                'kind': kind, 'value': value, 'reason': reason, 'family': family,
                'source_event_id': event['id'], 'available_at': event['end'],
                'generation': self.generation, 'argv': context['argv']}


def replay(events, scope):
    state = ExecutionEvidence(scope)
    timeline, missing_lines = [], []
    for event in events:
        if not (finite(event.get('start')) and finite(event.get('end')) and event['end'] >= event['start']):
            missing_lines.append(event['line'])
            continue
        timeline.extend(((event['start'], 0, event['line'], event), (event['end'], 1, event['line'], event)))
    result = {}
    for _, phase, _, raw in sorted(timeline, key=lambda item: item[:3]):
        event = {**raw, 'history_clock_complete': not any(line < raw['line'] for line in missing_lines)}
        if phase == 0:
            snapshots = state.begin(event)
            if snapshots:
                result[event['id']] = snapshots
        else:
            state.complete(event)
    return result


def failure_label(output):
    """Post-call labels for training/auditing only; never a begin() input."""
    if not isinstance(output, str):
        return None
    if re.search(r'\b\d+ (?:passed|failed|xfailed|xpassed)\b.*\bin \d', output):
        return None
    if re.search(r'(?m)^(?:/bin/(?:ba)?sh: \d+: |(?:ba)?sh: )(?:[^:\n]+): (?:not found|command not found)\s*$', output):
        return 'missing_executable'
    if re.search(r'(?m)^[^\n:]*python[^\n:]*: No module named ', output):
        return 'missing_module'
    if re.search(r'ConftestImportFailure|ImportError while loading conftest', output):
        return 'startup_import'
    if re.search(r'ERROR collecting|errors? during collection', output):
        return 'collection_import'
    return None


def exit_label(output):
    if not isinstance(output, str):
        return None
    match = re.search(r'(?:^|\n)Exit code: (-?\d+)\s*(?:\[Analyze the error above and try a different approach\.\]\s*)?$', output)
    return int(match.group(1)) if match else None


def failure_context(args):
    ctx = declared_context(args)
    if not ctx:
        return None
    argv = ctx['argv']
    name = ctx['entry'].rsplit('/', 1)[-1]
    if ctx['family'] == 'pytest':
        module, tail = 'pytest', argv[3:] if ctx['module'] else argv[1:]
    elif ctx['module'] in ('pip', 'unittest'):
        module, tail = ctx['module'], argv[3:]
    elif name == 'make' and argv[1:] == ['test']:
        module, tail = 'make:test', []
    else:
        return None
    return {'entry': ctx['entry'], 'module': module, 'cwd': ctx['cwd'],
            'env': json.dumps(ctx['env'], sort_keys=True),
            'scope': 'selected' if any(v.startswith(('tests/', './tests/', 'tests.')) for v in tail) else 'default',
            'operation': tail[0] if module == 'pip' and tail else '',
            'argv': argv}


def outcome(event):
    code = exit_label(event.get('output'))
    if code is None:
        return 'unknown'
    label = failure_label(event.get('output')) if code else None
    return label or ('success' if code == 0 else 'other_failure')


def setup(event):
    command = command_input(event['args']) or ''
    # A completed setup *attempt*, not a claim that installation succeeded.
    return bool(re.search(r'\b(?:pip\d?\s+(?:install|uninstall)|(?:apt(?:-get)?|conda|uv)\s+(?:install|remove|create|sync)|(?:venv|virtualenv)\b|(?:ln|export)\s)', command))


def failure_snapshot(event, history):
    ctx = failure_context(event['args']) if event['tool'] in ('exec', 'terminal_exec') else None
    if not ctx:
        return None
    features = {k: v for k, v in ctx.items() if k != 'argv'}
    features.update(last_same_runner='unknown', last_setup='unknown')
    sources = {}
    if finite(event.get('start')):
        prior = sorted((p for p in history if p['id'] != event['id'] and p['line'] < event['line']
                        and finite(p.get('start')) and finite(p.get('end'))
                        and p['start'] <= p['end'] < event['start']), key=lambda p: (p['end'], p['line']))
        for p in prior:
            if setup(p):
                features['last_setup'] = outcome(p)
                features['last_same_runner'] = 'unknown'
                sources['last_setup'] = p['id']
                sources.pop('last_same_runner', None)
            pc = failure_context(p['args']) if p['tool'] in ('exec', 'terminal_exec') else None
            if pc and all(pc[k] == ctx[k] for k in ('entry', 'module', 'cwd', 'env')):
                features['last_same_runner'] = outcome(p)
                sources['last_same_runner'] = p['id']
    return {'features': features, 'sources': sources}


class ContextHistory:
    """One live session, consumed in timestamp order; no raw output is retained."""
    def __init__(self, scope):
        self.evidence = ExecutionEvidence(scope)
        self.last_setup = None
        self.runners = {}
        self.pending = {}

    @staticmethod
    def runner_key(ctx):
        return json.dumps([ctx[k] for k in ('entry', 'module', 'cwd', 'env')], sort_keys=True)

    def begin(self, event):
        if event['id'] in self.pending:
            return self.pending[event['id']][1]
        declared = contexts(event['args']) if event['tool'] in ('exec', 'terminal_exec') else None
        failure = failure_snapshot(event, [])
        if failure is not None:
            f = failure['features']
            previous = self.runners.get(self.runner_key(f))
            for field, old in (('last_setup', self.last_setup), ('last_same_runner', previous)):
                if old and old['end'] < event['start']:
                    f[field] = old['outcome']
                    failure['sources'][field] = old['id']
        result = {'parsed': declared, 'snapshots': self.evidence.begin(event), 'failure': failure}
        self.pending[event['id']] = event, result
        return result

    def complete(self, event):
        old = self.pending.pop(event['id'], None)
        if old is None:
            return
        begin = old[0]
        event = {**begin, **event}
        self.evidence.complete(event)
        summary = {'id': event['id'], 'end': event['end'], 'outcome': outcome(event)}
        if setup(event):
            self.last_setup = summary
            self.runners.clear()
        ctx = failure_context(event['args']) if event['tool'] in ('exec', 'terminal_exec') else None
        if ctx:
            self.runners[self.runner_key(ctx)] = summary


def output_text(value):
    """Normalize common tool output envelopes, without executing their contents."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(output_text(v) for v in value)
    if not isinstance(value, dict):
        return ''
    text = '\n'.join(output_text(value[k]) for k in
                     ('text', 'stdout', 'stderr', 'content', 'output', 'result', 'details') if k in value)
    code = next((value[k] for k in ('exit_code', 'exitCode')
                 if type(value.get(k)) is int), None)
    return text + (f'\nExit code: {code}' if code is not None else '')
