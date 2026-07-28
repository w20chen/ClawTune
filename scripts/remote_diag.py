#!/usr/bin/env python3
"""Test script for diagnosing LLM API issues."""
import json
import urllib.request
import sys

API_KEY = "sk-08d88c7df9a048a29f6dae1cb3380565"
BASE_URL = "https://api.deepseek.com"

# Test 1: List models
print("=== Test 1: List available models ===")
try:
    req = urllib.request.Request(
        f"{BASE_URL}/v1/models",
        headers={"Authorization": f"Bearer {API_KEY}"}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    models = [m["id"] for m in data.get("data", [])]
    print(f"Available models ({len(models)}):")
    for m in models:
        print(f"  - {m}")
    print()
except Exception as e:
    print(f"ERROR listing models: {e}")
    print()

# Test 2: Test deepseek-v4-flash
print("=== Test 2: deepseek-v4-flash ===")
try:
    body = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "say hello in one word"}],
        "max_tokens": 50
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}"
        }
    )
    resp = urllib.request.urlopen(req, timeout=15)
    print(f"HTTP Status: {resp.status}")
    data = json.loads(resp.read())
    print(f"Response keys: {list(data.keys())}")
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        print(f"Content: {msg.get('content', '<EMPTY>')!r}")
        print(f"Finish reason: {choices[0].get('finish_reason')}")
    else:
        print("NO choices in response!")
    print(f"Usage: {data.get('usage')}")
    print(f"Full response: {json.dumps(data, indent=2)[:500]}")
except Exception as e:
    print(f"ERROR: {e}")

# Test 3: Test deepseek-chat
print()
print("=== Test 3: deepseek-chat ===")
try:
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "say hello in one word"}],
        "max_tokens": 50
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}"
        }
    )
    resp = urllib.request.urlopen(req, timeout=15)
    print(f"HTTP Status: {resp.status}")
    data = json.loads(resp.read())
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        print(f"Content: {msg.get('content', '<EMPTY>')!r}")
        print(f"Finish reason: {choices[0].get('finish_reason')}")
    else:
        print("NO choices!")
except Exception as e:
    print(f"ERROR: {e}")

# Test 4: Check trace file summary
print()
print("=== Test 4: Trace file summary ===")
import os
trace_dir = os.path.expanduser("~/claw/swe_rebench/traces/0b01001001__spectree-64")
for f in sorted(os.listdir(trace_dir)):
    if f.endswith(".jsonl"):
        fpath = os.path.join(trace_dir, f)
        with open(fpath) as fh:
            lines = fh.readlines()
        print(f"Trace: {f} ({len(lines)} lines)")
        record_types = {}
        for line in lines:
            try:
                rec = json.loads(line)
                rt = rec.get("record_type") or rec.get("type") or "unknown"
                record_types[rt] = record_types.get(rt, 0) + 1
            except:
                pass
        print(f"  Record types: {record_types}")
        
        # Count LLM span ends and check output
        llm_ends = 0
        empty_llm = 0
        for line in lines:
            try:
                rec = json.loads(line)
                if rec.get("record_type") == "span_end" and rec.get("kind") == "llm":
                    llm_ends += 1
                    output = rec.get("output", {})
                    if isinstance(output, dict):
                        content = output.get("content")
                        if isinstance(content, str) and not content.strip():
                            empty_llm += 1
                        elif isinstance(content, list) and len(content) == 0:
                            empty_llm += 1
                        elif content is None:
                            empty_llm += 1
            except:
                pass
        print(f"  LLM span_ends: {llm_ends}, empty: {empty_llm}")
        break
