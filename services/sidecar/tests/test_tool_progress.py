import pytest

from clawtune_sidecar.tool_progress import (
    CargoProgress,
    CMakeProgress,
    CurlProgress,
    FfmpegProgress,
    GitProgress,
    MakeProgress,
    NinjaProgress,
    NpmProgress,
    PipProgress,
    PytestProgress,
    PythonProgress,
    RsyncProgress,
    ToolProgress,
    WgetProgress,
    adapter_for,
)


@pytest.mark.parametrize("command,kind", [
    ("curl -o result https://example.test/file", CurlProgress),
    ("wget -O result https://example.test/file", WgetProgress),
    ("pytest -v tests", PytestProgress),
    ("/venv/bin/python3.12 -m pytest -v tests", PytestProgress),
    ("ninja -C build", NinjaProgress),
    ("cmake --build build", CMakeProgress),
    ("rsync --info=progress2 source dest", RsyncProgress),
    ("ffmpeg -i movie.mp4 -c:v libx264 out.mp4", FfmpegProgress),
    ("pip install numpy", PipProgress), ("python -m pip install numpy", PipProgress),
    ("git clone https://example.test/repo", GitProgress), ("npm install", NpmProgress),
    ("cargo build", CargoProgress), ("python script.py", PythonProgress),
    ("make all", MakeProgress), ("sleep 5", ToolProgress),
    ("curl https://example.test/file && pytest", ToolProgress),
    ("curl https://example.test/file | wc", ToolProgress),
    ("wget -r https://example.test/", ToolProgress),
    ("curl https://example.test/a https://example.test/b", ToolProgress),
    ("ffmpeg -i x -vf setpts=2*PTS out", ToolProgress),
    ("ffmpeg -i x out1 out2", ToolProgress),
    ("rsync --progress source dest", ToolProgress),
])
def test_each_tool_selects_its_own_observer(command, kind):
    assert type(adapter_for(command)) is kind


def test_curl_reads_native_remaining_field_not_total_duration():
    adapter = CurlProgress()
    result = adapter.observe(" 80 1024k 80 819k 0 0 200k 0 0:00:05 0:00:04 0:00:01 200k", 99)
    assert result.seconds == 1
    assert result.source == "curl.time_left"
    assert result.kind == "estimated"
    assert adapter.observe(" 100 1024k 100 1024k 0 0 200k 0 0:00:05 0:00:05 0:00:00 200k", 99) is None


def test_curl_reads_compact_time_left_from_curl_8_18():
    progress = CurlProgress()
    value = progress.observe(
        "31 512.0k 31 160.0k 0 0 147.6k 0 00:03 00:01 00:02 147.7k", 1)
    assert value.seconds == 2
    assert progress.observe(
        "87 512.0k 87 448.0k 0 0 137.8k 0 00:03 00:03 137.8k", 3) is None


def test_wget_distinguishes_eta_from_completed_elapsed_time():
    adapter = WgetProgress()
    assert adapter.observe(" 50K .......... .......... 40% 512K 2s", 1).seconds == 2
    assert adapter.observe("out 90%[======> ] 900K 200KB/s eta 1m2s", 1).seconds == 62
    assert adapter.observe("out 100%[=======] 1M 200KB/s in 1s", 1) is None


@pytest.mark.parametrize("adapter,first,second", [
    (PytestProgress, "test_a PASSED [ 50%]", "test_b PASSED [ 75%]"),
    (NinjaProgress, "[2/4] CXX a", "[3/4] CXX b"),
    (CMakeProgress, "[ 50%] Built target a", "[ 75%] Built target b"),
])
def test_counts_use_only_this_execution_and_ignore_repeated_updates(adapter, first, second):
    progress = adapter()
    assert progress.observe(first, 1) is None
    assert progress.observe(first, 1.9) is None
    remaining = progress.observe(second, 2)
    assert remaining.seconds == 1
    assert remaining.kind == "estimated"
    assert progress.observe(second, 3) is None


def test_rsync_does_not_use_unfinished_file_discovery():
    progress = RsyncProgress()
    assert progress.observe(" 100,000 50% 1.00MB/s 0:00:02 (xfr#1, ir-chk=2/3)", 1) is None
    assert progress.observe(" 100,000 50% 1.00MB/s 0:00:02", 1) is None
    assert progress.observe(" 100,000 50% 1.00MB/s 0:00:02 (xfr#1, to-chk=2/3)", 1) is None
    assert progress.observe(" 110,000 55% 1.00MB/s 0:00:02", 2).seconds == 2


def test_ffmpeg_uses_media_progress_and_current_processing_speed():
    progress = FfmpegProgress()
    assert progress.observe("Duration: 00:00:10.00, start: 0.000", 0) is None
    assert progress.observe("frame= 90 time=00:00:08.00 bitrate=100kbits/s speed=2.0x", 8).seconds == 1


@pytest.mark.parametrize("adapter", [ToolProgress, PipProgress, GitProgress, NpmProgress,
                                     CargoProgress, PythonProgress, MakeProgress])
def test_unavailable_tools_never_interpret_subtask_progress(adapter):
    progress = adapter()
    assert progress.exact_remaining() is None
    assert progress.observe("download 99% ETA 00:01", 10) is None
