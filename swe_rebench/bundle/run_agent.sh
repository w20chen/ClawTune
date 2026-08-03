#!/bin/bash
set -euo pipefail
echo "[claw] running agent (fallback)..."
echo "[claw] TASK_INSTANCE_ID=${TASK_INSTANCE_ID:-unknown}"
# OpenClaw 2026.7.x uses `--agent main`; newer builds moved the agent id to a
# positional subcommand (`openclaw agent main ...`).  Match the installed CLI.
if openclaw agent --help 2>&1 | grep -q -- '--agent'; then
    exec openclaw agent --local \
        --agent main \
        --model "vllm/deepseek-v4-flash" \
        --message "${PROBLEM_STATEMENT:-Solve the task.}"
else
    exec openclaw agent main --local \
        --model "vllm/deepseek-v4-flash" \
        --message "${PROBLEM_STATEMENT:-Solve the task.}"
fi
