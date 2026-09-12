from io import StringIO

from swe_rebench.console import PREFIX, tee_agent_output


def test_console_only_has_bucket_rows_while_log_preserves_every_byte():
    raw = ("[openclaw] turn 1: model\n[openclaw] prediction\n"
           "  CPU peak: unavailable\n  evidence: historical=[]\n"
           + PREFIX + "Time buckets | call-1\n"
           + PREFIX + "runtime (mode)             call       #2 [500ms,2s)\n"
           + "[openclaw] tool exec: ok\nAgent answer\n")
    saved, visible = StringIO(), []
    tee_agent_output(StringIO(raw), saved, visible.append)
    assert saved.getvalue() == raw
    assert visible == ["Time buckets | call-1", "runtime (mode)             call       #2 [500ms,2s)"]


def test_unmarked_stderr_and_partial_last_line_are_retained():
    saved, visible = StringIO(), []
    tee_agent_output(StringIO("warning\nerror without newline"), saved, visible.append)
    assert saved.getvalue() == "warning\nerror without newline"
    assert not visible
