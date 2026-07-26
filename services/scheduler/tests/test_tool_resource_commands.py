from __future__ import annotations

import agent_scheduler.tool_resource_commands as commands


def test_operation_uses_tool_resource_clause_identity(monkeypatch) -> None:
    def parse_command(command: str) -> dict:
        assert command == "python -m pytest tests -q"
        return {
            "clauses": [
                {"bin": "python", "argv": ["python", "-m", "pytest", "tests", "-q"]}
            ],
            "parse_failed": False,
        }

    monkeypatch.setattr(commands, "parse_command_clauses", parse_command)

    assert (
        commands.operation_from_tool_request(
            "exec",
            {"command": "python -m pytest tests -q"},
        )
        == "python"
    )


def test_operation_is_unknown_for_compound_commands(monkeypatch) -> None:
    monkeypatch.setattr(
        commands,
        "parse_command_clauses",
        lambda _command: {
            "clauses": [
                {"bin": "python", "argv": ["python", "-m", "pytest"]},
                {"bin": "git", "argv": ["git", "status"]},
            ],
            "parse_failed": False,
        },
    )

    assert (
        commands.operation_from_tool_request(
            "exec",
            {"command": "python -m pytest && git status"},
        )
        is None
    )


def test_extract_command_reads_nested_exec_params() -> None:
    assert commands.extract_command({"exec": {"cmd": "git status"}}) == "git status"
