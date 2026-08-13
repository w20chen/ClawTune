#!/usr/bin/env python3
"""Diagnose an OpenAI-compatible endpoint without storing credentials."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Sequence


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODELS = ("deepseek-v4-flash", "deepseek-chat")


def request_json(
    url: str,
    api_key: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("endpoint returned a non-object JSON response")
    return body


def inspect_trace_directory(trace_dir: Path) -> None:
    if not trace_dir.is_dir():
        print(f"Trace directory does not exist: {trace_dir}")
        return
    files = sorted(trace_dir.glob("*.jsonl"))
    if not files:
        print(f"No JSONL traces found in: {trace_dir}")
        return

    path = files[0]
    record_types: Counter[str] = Counter()
    llm_ends = 0
    empty_llm = 0
    line_count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line_count += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record_type = str(record.get("record_type") or record.get("type") or "unknown")
        record_types[record_type] += 1
        if record_type != "span_end" or record.get("kind") != "llm":
            continue
        llm_ends += 1
        output = record.get("output")
        content = output.get("content") if isinstance(output, dict) else None
        if content is None or content == [] or (isinstance(content, str) and not content.strip()):
            empty_llm += 1

    print(f"Trace: {path} ({line_count} lines)")
    print(f"Record types: {dict(record_types)}")
    print(f"LLM span ends: {llm_ends}; empty: {empty_llm}")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--base-url",
        default=os.getenv("CLAWTUNE_LLM_UPSTREAM_BASE_URL", DEFAULT_BASE_URL),
        help="OpenAI-compatible API base URL.",
    )
    cli.add_argument(
        "--api-key-env",
        default="LLM_API_KEY",
        help="Name of the environment variable containing the API key.",
    )
    cli.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Model to probe; repeat for multiple models.",
    )
    cli.add_argument(
        "--trace-dir",
        type=Path,
        help="Optional trace directory to summarize after the endpoint checks.",
    )
    return cli


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    api_key = os.getenv(args.api_key_env)
    if not api_key:
        print(
            f"Missing API key: export {args.api_key_env} before running this diagnostic.",
            file=sys.stderr,
        )
        return 2

    base_url = args.base_url.rstrip("/")
    models = tuple(args.models or DEFAULT_MODELS)
    failed = False
    try:
        response = request_json(f"{base_url}/v1/models", api_key)
        available = [
            item.get("id")
            for item in response.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        print(f"Available models ({len(available)}): {', '.join(available)}")
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        failed = True
        print(f"Model-list request failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    for model in models:
        try:
            response = request_json(
                f"{base_url}/v1/chat/completions",
                api_key,
                payload={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say hello in one word."}],
                    "max_tokens": 50,
                },
            )
            choices = response.get("choices")
            content = None
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = message.get("content")
            print(f"{model}: OK; content={content!r}")
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            failed = True
            print(f"{model}: FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)

    if args.trace_dir is not None:
        inspect_trace_directory(args.trace_dir.expanduser().resolve())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
