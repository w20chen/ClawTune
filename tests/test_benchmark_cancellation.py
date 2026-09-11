import subprocess
import sys

import pytest

from swe_rebench.cancellation import (
    Cancellation, TaskCancelled, cancellation_scope, check_cancelled, run_command,
)


def test_setup_command_retries_communicate_without_duplicating_input():
    with cancellation_scope(Cancellation()):
        result = run_command(
            [sys.executable, "-c",
             "import sys,time; data=sys.stdin.read(); time.sleep(.25); sys.stdout.write(data)"],
            input="task configuration", text=True, capture_output=True, check=True,
            timeout=3,
        )
    assert result.stdout == "task configuration"
    assert result.returncode == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_setup_command_terminates_process_on_cancel_or_timeout(monkeypatch, cancel):
    cancellation = Cancellation()
    popen = subprocess.Popen
    processes = []

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        if cancel:
            cancellation.cancel()
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    with cancellation_scope(cancellation):
        with pytest.raises(TaskCancelled if cancel else subprocess.TimeoutExpired):
            run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                        timeout=0.1, capture_output=True)
    assert len(processes) == 1
    assert processes[0].poll() is not None
    # A cancelled worker must not contaminate the next use of its thread.
    check_cancelled()


def test_precancelled_scope_does_not_start_command(monkeypatch):
    cancellation = Cancellation()
    cancellation.cancel()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("launched after cancellation"))
    with pytest.raises(TaskCancelled), cancellation_scope(cancellation):
        run_command([sys.executable, "-c", "pass"])
    check_cancelled()
