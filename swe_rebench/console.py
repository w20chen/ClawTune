"""Console projection only; full agent output is always retained in trace logs."""
from __future__ import annotations

from typing import Callable, Iterable, TextIO
from pathlib import Path
from threading import Event

PREFIX = "[openclaw] [clawtune-time-buckets] "


def follow_agent_output(path: Path, stop: Event, emit: Callable[[str], None]) -> None:
    """Project a regular log file without waiting for descendant pipe EOF."""
    with path.open(encoding="utf-8", errors="replace") as stream:
        pending = ""
        final_size = None
        dropping = False
        while True:
            # Snapshot the stop flag before reading so the final pass drains
            # bytes already written when the agent finished.
            finished = stop.is_set()
            if finished and final_size is None:
                final_size = path.stat().st_size
            chunk = stream.read(65536)
            empty_read = not chunk
            if dropping:
                _, separator, chunk = chunk.partition("\n")
                dropping = not separator
            pending += chunk
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                if line.startswith(PREFIX):
                    emit(line[len(PREFIX):].rstrip("\r"))
            # Bound console buffering for arbitrarily long non-console lines.
            if len(pending) > 65536:
                pending = ""
                dropping = True
            if final_size is not None and (empty_read or stream.tell() >= final_size):
                if pending.startswith(PREFIX):
                    emit(pending[len(PREFIX):].rstrip("\r"))
                return
            if empty_read:
                stop.wait(0.05)


def tee_agent_output(lines: Iterable[str], log_file: TextIO, emit: Callable[[str], None]) -> None:
    for line in lines:
        log_file.write(line)
        log_file.flush()
        if line.startswith(PREFIX):
            emit(line[len(PREFIX):].rstrip("\r\n"))
