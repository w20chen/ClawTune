"""Authenticated loopback bridge for task-owned tools; never changes OpenClaw core."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ToolBridge:
    def __init__(self, backend):
        self.backend = backend
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.results = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/call" or self.headers.get("Authorization") != "Bearer " + owner.token:
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 8 * 1024 * 1024:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(size))
                    if not all(isinstance(body.get(k), str) and body[k] for k in ("name", "call_id")) or not isinstance(body.get("arguments"), dict):
                        raise ValueError("invalid tool call")
                    signature = json.dumps([body["name"], body["arguments"]], sort_keys=True)
                    with owner.lock:
                        key = body["call_id"]
                        if key in owner.results:
                            prior, result = owner.results[key]
                            if signature != prior:
                                raise ValueError("conflicting duplicate tool call")
                        else:
                            result = owner.backend.call(body["name"], body["arguments"])
                            owner.results[key] = (signature, result)
                    data = json.dumps({"result": result}, default=str, allow_nan=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as exc:
                    self.send_error(400, str(exc))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def manifest(self, path: Path):
        from clawtune_kb.store import write_json
        from clawtune_kb.contracts import validate
        value = {"schema": "clawtune.tool-bridge.v1", "endpoint": f"http://127.0.0.1:{self.server.server_port}/call",
                 "token": self.token, "tools": self.backend.tools}
        validate(value, "tool-bridge.schema.json")
        write_json(path, value)
        path.chmod(0o600)

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
