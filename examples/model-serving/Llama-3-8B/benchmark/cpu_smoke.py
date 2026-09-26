#!/usr/bin/env python3
"""Validate RF completions request shape and failure propagation on CPU."""

from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).with_name("benchmark.py")


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _FakeServer

    def do_POST(self) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            error = self.server.validate(self.path, body)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            error, body = f"invalid request: {exc}", {}
        with self.server.lock:
            self.server.requests.append(body)
            failing = self.server.failure_mode
        with self.server.service_lock:
            if error:
                self.send_error(400, error)
                return
            if failing:
                self.send_error(503, "injected CPU fake failure")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for token in range(body["max_tokens"]):
                event = {"choices": [{"text": "x", "token_ids": [token], "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.001)
            end = {"choices": [{"text": "", "token_ids": [], "finish_reason": "length"}]}
            self.wfile.write(b"data: " + json.dumps(end).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], input_tokens: int, output_tokens: int, model: str
    ) -> None:
        super().__init__(address, _Handler)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model
        self.requests: list[dict[str, Any]] = []
        self.failure_mode = False
        self.lock = threading.Lock()
        self.service_lock = threading.Lock()

    def validate(self, path: str, body: Any) -> str | None:
        if path != "/v1/completions":
            return f"unexpected endpoint {path!r}"
        if body.get("model") != self.model:
            return f"unexpected model {body.get('model')!r}"
        prompt = body.get("prompt")
        if not isinstance(prompt, list) or len(prompt) != self.input_tokens:
            return f"prompt must contain {self.input_tokens} token IDs"
        if any(isinstance(value, bool) or not isinstance(value, int) for value in prompt):
            return "prompt must contain integer token IDs"
        if body.get("max_tokens") != self.output_tokens:
            return f"max_tokens must be {self.output_tokens}"
        if body.get("stream") is not True or body.get("temperature") != 0:
            return "expected streaming greedy completions"
        return None


def _invoke(
    args: argparse.Namespace, server: _FakeServer, output_path: Path, should_succeed: bool
) -> None:
    command = [
        sys.executable,
        str(_SCRIPT),
        "--request-factory-engine",
        args.engine,
        "--url",
        f"http://127.0.0.1:{server.server_port}",
        "--model",
        server.model,
        "--tokenizer",
        args.tokenizer,
        "--request-count",
        "32",
        "--input-tokens",
        str(server.input_tokens),
        "--output-tokens",
        str(server.output_tokens),
        "--concurrencies",
        "1",
        "--vs-output",
        str(output_path),
    ]
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if (result.returncode == 0) != should_succeed:
        raise RuntimeError(
            f"unexpected benchmark status {result.returncode}\n{result.stdout}\n{result.stderr}\nlast request={server.requests[-1:]!r}"
        )
    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    if records[0] != {
        "kind": "hello",
        "protocol": 2,
        "metrics": {
            "aggregate_throughput": {"unit": "tok/s", "direction": "max"},
            "p99_latency_ms": {"unit": "ms", "direction": "min"},
        },
    }:
        raise RuntimeError(f"unexpected VibeSys result protocol handshake: {records[0]}")
    if should_succeed and records[-1].get("kind") != "result":
        raise RuntimeError(f"success did not produce result record: {records[-1]}")
    if not should_succeed and (
        records[-1].get("kind") != "error"
        or "failed requests" not in records[-1].get("message", "")
    ):
        raise RuntimeError(f"RF failures were not surfaced: {records[-1]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", dest="engine", required=True)
    parser.add_argument("--tokenizer", required=True, help="local tokenizer.json for the CPU trace")
    args = parser.parse_args()
    server = _FakeServer(("127.0.0.1", 0), 16, 4, "cpu-smoke-model")
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="llama3-rf-cpu-") as directory:
            output = Path(directory) / "success.jsonl"
            _invoke(args, server, output, True)
            if len(server.requests) != 64:
                raise RuntimeError(
                    f"expected 64 requests across the point and its confirmation, observed {len(server.requests)}"
                )
            server.requests.clear()
            server.failure_mode = True
            _invoke(args, server, Path(directory) / "failure.jsonl", False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("CPU completions smoke passed: sweep requests, VibeSys metrics, and failure rejection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
