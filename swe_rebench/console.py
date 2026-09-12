"""Console projection only; full agent output is always retained in trace logs."""
from __future__ import annotations

from typing import Callable, Iterable, TextIO

PREFIX = "[openclaw] [clawtune-time-buckets] "


def tee_agent_output(lines: Iterable[str], log_file: TextIO, emit: Callable[[str], None]) -> None:
    for line in lines:
        log_file.write(line)
        log_file.flush()
        if line.startswith(PREFIX):
            emit(line[len(PREFIX):].rstrip("\r\n"))
