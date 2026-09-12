"""Linux agent supervisor: retain ownership of detached/orphaned descendants."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def identity(pid: int):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19], fields[0]
    except (OSError, IndexError):
        return None


def children(pid: int) -> set[int]:
    result = set()
    for task in Path(f"/proc/{pid}/task").glob("*/children"):
        try:
            result.update(map(int, task.read_text().split()))
        except OSError:
            pass
    return result


def signal_owned(pid: int, start: str, sig: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        current = identity(pid)
        if current is not None and current[0] == start:
            signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def cleanup(timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        # Stop each owned parent before enumerating its children: it can no
        # longer fork away while the rest of its subtree is being collected.
        pending = list(children(os.getpid()))
        owned = {}
        while pending:
            pid = pending.pop()
            if pid in owned:
                continue
            current = identity(pid)
            if current is None:
                continue
            owned[pid] = current[0]
            if current[1] != "Z":
                try:
                    signal_owned(pid, current[0], signal.SIGSTOP)
                except ProcessLookupError:
                    continue
            pending.extend(children(pid))
        for pid, start in reversed(list(owned.items())):
            current = identity(pid)
            if current is not None and current[0] == start and current[1] != "Z":
                try:
                    signal_owned(pid, start, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if not children(os.getpid()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def main() -> int:
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot establish agent subreaper")
    stopping = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda number, _frame: stopping.append(number))
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    process = subprocess.Popen(argv)
    try:
        while process.poll() is None and not stopping:
            time.sleep(0.05)
        code = process.returncode if not stopping else 128 + stopping[0]
    finally:
        try:
            clean = cleanup()
        except Exception as exc:
            print(f"agent cleanup failed: {exc}", file=sys.stderr)
            clean = False
        if not clean:
            print("agent descendants did not exit", file=sys.stderr)
            return 125
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
