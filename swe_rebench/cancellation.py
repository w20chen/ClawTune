"""Cooperative cancellation shared by benchmark workers and host executors."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
import subprocess
import threading
import time


class TaskCancelled(RuntimeError):
    pass


class Cancellation:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def check(self):
        if self._event.is_set():
            raise TaskCancelled("benchmark cancelled")


_current: ContextVar[Cancellation | None] = ContextVar("task_cancellation", default=None)


@contextmanager
def cancellation_scope(cancellation):
    token = _current.set(cancellation)
    try:
        check_cancelled()
        yield
    finally:
        _current.reset(token)


def check_cancelled():
    cancellation = _current.get()
    if cancellation is not None:
        cancellation.check()


def wait_process(process, timeout):
    """Poll for cancellation even when the agent has no configured timeout."""
    if _current.get() is None:
        return process.wait(timeout=timeout)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        check_cancelled()
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.wait(timeout=0.1 if remaining is None else min(0.1, remaining))
        except subprocess.TimeoutExpired:
            pass


def run_command(args, *, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """Cancellable subprocess.run for task setup, preserving legacy callers."""
    if _current.get() is None:
        return subprocess.run(args, input=input, capture_output=capture_output,
                              timeout=timeout, check=check, **kwargs)
    check_cancelled()
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    kwargs["start_new_session"] = os.name == "posix"
    deadline = None if timeout is None else time.monotonic() + timeout
    with subprocess.Popen(args, **kwargs) as process:
        try:
            while True:
                check_cancelled()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(
                        input=input, timeout=0.1 if remaining is None else min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    # communicate retains its input buffer across retries.
                    input = None
        except BaseException:
            from swe_rebench.host_openclaw import _kill_agent_process_and_confirm
            _kill_agent_process_and_confirm(process)
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
