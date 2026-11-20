"""Per-tool, current-execution progress. No history or predicted-total countdown."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RemainingTime:
    seconds: float
    source: str
    kind: str = "estimated"


class ToolProgress:
    """Empty interface for tools without observable whole-call progress.

    Implement exact_remaining only when a tool API guarantees remaining wall
    time. A timeout, requested sleep duration, percent or ETA is not that proof.
    """

    output_fd = 1

    def exact_remaining(self) -> RemainingTime | None:
        return None

    def observe(self, line: str, elapsed: float) -> RemainingTime | None:
        return None


def clock_seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


class CurlProgress(ToolProgress):
    output_fd = 2

    def observe(self, line, elapsed):
        # curl's regular meter: % total ... time-total time-spent time-left speed.
        fields = line.split()
        if (len(fields) == 12 and fields[0].isdigit() and 0 < int(fields[0]) < 100
                and re.fullmatch(r"(?:\d+:)?\d\d:\d\d", fields[10])):
            return RemainingTime(clock_seconds(fields[10]), "curl.time_left")
        return None


class WgetProgress(ToolProgress):
    output_fd = 2

    def observe(self, line, elapsed):
        # Both dot and forced-bar formats report their own ETA. Reject 100%:
        # the trailing duration on the final line is elapsed time, not ETA.
        percent = re.search(r"\b(\d+)%", line)
        if not percent or not 0 < int(percent[1]) < 100:
            return None
        match = re.search(r"(?:eta\s+)?((?:\d+h)?(?:\d+m)?\d+s)\s*$", line)
        if match:
            seconds = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[unit]
                          for n, unit in re.findall(r"(\d+)([hms])", match[1]))
            return RemainingTime(seconds, "wget.eta")
        return None


class CountProgress(ToolProgress):
    """Rate from two observed updates; never use the pre-execution prediction."""

    def __init__(self):
        self.previous: tuple[float, float, float] | None = None

    def update(self, completed, total, elapsed, source):
        previous = self.previous
        if previous is not None and previous[:2] == (completed, total):
            return None
        self.previous = (completed, total, elapsed)
        if (previous is None or previous[1] != total or not previous[0] < completed < total
                or elapsed <= previous[2]):
            return None
        seconds = (total - completed) * (elapsed - previous[2]) / (completed - previous[0])
        return RemainingTime(seconds, source)


class PytestProgress(CountProgress):
    def observe(self, line, elapsed):
        match = re.search(r"\[\s*(\d+)%\]\s*$", line)
        if match:
            return self.update(int(match[1]), 100, elapsed, "pytest.completed_percent")
        return None


class NinjaProgress(CountProgress):
    def observe(self, line, elapsed):
        match = re.match(r"\[(\d+)/(\d+)\]\s", line)
        if match:
            return self.update(int(match[1]), int(match[2]), elapsed, "ninja.completed_edges")
        return None


class CMakeProgress(CountProgress):
    def observe(self, line, elapsed):
        match = re.match(r"\[\s*(\d+)%\]\s", line)
        if match:
            return self.update(int(match[1]), 100, elapsed, "cmake.completed_percent")
        # cmake --build may use Ninja instead of Makefiles.
        match = re.match(r"\[(\d+)/(\d+)\]\s", line)
        if match:
            return self.update(int(match[1]), int(match[2]), elapsed, "cmake.ninja_edges")
        return None


class RsyncProgress(ToolProgress):
    def __init__(self, stable_total=False):
        self.stable_total = stable_total

    def observe(self, line, elapsed):
        # Only selected for --info=progress2. Incremental file-list discovery
        # (ir-chk) does not yet have a stable total; --progress is per-file.
        # rsync progress.c switches to elapsed time on xfr# completion rows,
        # even if the overall percent is below 100. Never parse those as ETA.
        if "(xfr#" in line:
            self.stable_total = "to-chk=" in line
            return None
        match = re.search(r"\s(\d+)%\s+\S+/s\s+(\d+:\d\d:\d\d)", line)
        if match and self.stable_total and 0 < int(match[1]) < 100:
            return RemainingTime(clock_seconds(match[2]), "rsync.overall_eta")
        return None


class FfmpegProgress(ToolProgress):
    output_fd = 2

    def __init__(self):
        self.duration = None

    def observe(self, line, elapsed):
        duration = re.search(r"Duration:\s*(\d+:\d\d:\d\d\.\d+)", line)
        if duration:
            self.duration = clock_seconds(duration[1])
        match = re.search(r"time=(\d+:\d\d:\d\d\.\d+).*speed=\s*([\d.]+)x", line)
        if match and self.duration is not None and float(match[2]) > 0:
            return RemainingTime((self.duration - clock_seconds(match[1])) / float(match[2]),
                                 "ffmpeg.media_time_and_speed")
        return None


# These tools have no generally usable whole-call progress interface. Their
# individual classes are extension points, not simulated time implementations.
class PipProgress(ToolProgress):
    """Download bars omit dependency resolution, builds and installation."""


class GitProgress(ToolProgress):
    """Transfer/object counts omit later checkout, filters and hooks."""


class NpmProgress(ToolProgress):
    """Install stages omit arbitrary lifecycle scripts."""


class CargoProgress(ToolProgress):
    """Crate counts do not cover build scripts and final linking."""


class PythonProgress(ToolProgress):
    """An arbitrary script's tqdm bar can describe an inner loop only."""


class MakeProgress(ToolProgress):
    """Generic Makefiles have no standard whole-build progress protocol."""


_EMPTY_ADAPTERS = {
    "pip": PipProgress, "pip3": PipProgress, "git": GitProgress,
    "npm": NpmProgress, "npx": NpmProgress, "yarn": NpmProgress, "pnpm": NpmProgress,
    "cargo": CargoProgress, "python": PythonProgress, "python3": PythonProgress,
    "make": MakeProgress, "gmake": MakeProgress,
}


def adapter_for(command: str) -> ToolProgress:
    # A subcommand's progress must never finish a pipeline/serial tool call.
    # Keep the original command untouched; only parse it to select an observer.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        args = list(lexer)
    except ValueError:
        return ToolProgress()
    if any(token in {";", "&&", "||", "|", "&", "(", ")", "<", ">", ">>", "<<"}
           for token in args) or "\n" in command:
        return ToolProgress()
    while args and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", args[0]):
        args.pop(0)
    if not args:
        return ToolProgress()
    name = Path(args[0]).name
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", name):
        if args[1:2] == ["-m"] and len(args) > 2:
            name, args = args[2], args[2:]
        else:
            return PythonProgress()
    urls = [arg for arg in args if re.search(r"(?:^|=)https?://", arg)]
    if name in {"curl", "wget"}:
        # One explicit URL, no batch/recursive/parallel requests or URL globbing.
        blocked = {"--next", "--parallel", "-Z", "-r", "--recursive", "-m", "--mirror",
                   "-i", "--input-file", "-K", "--config"}
        if (len(urls) != 1 or any(arg.split("=", 1)[0] in blocked for arg in args[1:])
                or any(char in urls[0] for char in "{}[]")):
            return ToolProgress()
        return CurlProgress() if name == "curl" else WgetProgress()
    if name == "pytest":
        return PytestProgress()
    if name == "ninja":
        return NinjaProgress()
    if name == "cmake" and "--build" in args:
        return CMakeProgress()
    if name == "rsync" and any(arg.startswith("--info=") and "progress2" in arg for arg in args):
        return RsyncProgress(stable_total="--no-inc-recursive" in args or "--no-i-r" in args)
    if name == "ffmpeg" and args.count("-i") == 1:
        # Duration-changing transforms or multiple outputs need separate logic.
        blocked = {"-ss", "-sseof", "-t", "-to", "-vf", "-af", "-filter_complex",
                   "-filter:v", "-filter:a", "-stream_loop", "-shortest"}
        value_options = {"-i", "-c", "-c:v", "-c:a", "-codec", "-preset", "-crf",
                         "-b:v", "-b:a", "-f", "-threads", "-loglevel", "-v"}
        flags = {"-y", "-n", "-hide_banner", "-nostdin", "-an", "-vn"}
        outputs, index = [], 1
        while index < len(args):
            if args[index] in value_options:
                index += 2
            elif args[index] in flags:
                index += 1
            elif args[index].startswith("-"):
                return ToolProgress()
            else:
                outputs.append(args[index])
                index += 1
        if not blocked.intersection(args) and len(outputs) == 1:
            return FfmpegProgress()
    return _EMPTY_ADAPTERS.get(name, ToolProgress)()
