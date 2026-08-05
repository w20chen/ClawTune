#!/bin/bash
set -euo pipefail
echo "[claw] running agent (fallback)..."
echo "[claw] TASK_INSTANCE_ID=${TASK_INSTANCE_ID:-unknown}"
# OpenClaw 2026.7.x uses `--agent main`; newer builds moved the agent id to a
# positional subcommand (`openclaw agent main ...`).  Match the installed CLI.
# A flag build answers `agent main --help` with the parent usage or the "Too
# many arguments ... Try: openclaw agent main --help" hint (which also contains
# "agent main"), so only the real subcommand usage line on stdout proves a
# positional build.
if openclaw agent main --help 2>/dev/null | grep -q 'Usage: openclaw agent main'; then
    exec openclaw agent main --local \
        --model "vllm/deepseek-v4-flash" \
        --message "${PROBLEM_STATEMENT:-Solve the task.}"
else
    exec openclaw agent --local \
        --agent main \
        --model "vllm/deepseek-v4-flash" \
        --message "${PROBLEM_STATEMENT:-Solve the task.}"
fi
