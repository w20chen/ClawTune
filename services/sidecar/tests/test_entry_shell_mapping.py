"""An exact entry shell may exec the command in place without owning its work."""
from dataclasses import replace

import pytest

from tool_resource.clause_bridge import ExecImageRecord, bridge_command


def image(seq, argv, *, terminal=True):
    return ExecImageRecord(
        host_pid=42, exec_seq=seq, t_exec_ns=seq * 1_000_000_000,
        t_end_ns=(seq + 1) * 1_000_000_000, bin=argv[0].rsplit('/', 1)[-1],
        argv=argv, terminal=terminal, cpu_windows=(), rss_bins=(),
        normal_exit_status=0 if terminal else None,
        requested_executable_path=argv[0],
    )


def test_entry_shell_exec_in_place_maps_only_the_workload():
    command = 'python3 work.py'
    wrapper = image(1, ('/bin/sh', '-c', command), terminal=False)
    workload = image(2, ('python3', 'work.py'))
    result = bridge_command('repo', command, [wrapper, workload],
                            entry_pid=42, fork_parent={})
    assert result.data_valid
    assert len(result.bridged) == 1
    clause = result.bridged[0]
    assert clause.owned_exec_images == ((42, 2),)
    assert clause.observation.ts_start == 2
    assert clause.observation.ts_end == 3
    assert clause.observation.argv == ('python3', 'work.py')
    assert {'kind': 'same_pid_exec', 'from': [42, 1], 'to': [42, 2]} in result.transition_graph


@pytest.mark.parametrize('case', ['different_command', 'truncated', 'not_entry', 'not_shell'])
def test_unverified_prefix_cannot_be_skipped(case):
    command = 'python3 work.py'
    wrapper = image(1, ('/bin/sh', '-c', command), terminal=False)
    entry_pid = 42
    if case == 'different_command':
        wrapper = replace(wrapper, argv=('/bin/sh', '-c', 'other.py'))
    elif case == 'truncated':
        wrapper = replace(wrapper, argv_capped=True)
    elif case == 'not_entry':
        entry_pid = 1
    elif case == 'not_shell':
        wrapper = replace(wrapper, bin='env', argv=('env', 'python3', 'work.py'))
    result = bridge_command('repo', command, [wrapper, image(2, ('python3', 'work.py'))],
                            entry_pid=entry_pid, fork_parent={})
    assert not result.data_valid
    assert not result.bridged


@pytest.mark.parametrize('argv', [('env', 'python3', 'work.py'), ('sh', 'work.sh')])
def test_explicit_wrapper_remains_the_clause_identity(argv):
    command = ' '.join(argv)
    images = [image(1, ('/bin/sh', '-c', command), terminal=False),
              image(2, argv, terminal=False), image(3, ('python3', 'work.py'))]
    result = bridge_command('repo', command, images, entry_pid=42, fork_parent={})
    assert result.data_valid
    assert len(result.bridged) == 1
    assert result.bridged[0].observation.argv == argv
    assert result.bridged[0].owned_exec_images == ((42, 2), (42, 3))
