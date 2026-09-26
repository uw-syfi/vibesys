#!/usr/bin/env python3
"""Validate the Trainium RF matrix against a CPU-only completions fake."""

from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import sys
import tempfile
import threading
from collections import Counter
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
            body, error = {}, f"invalid request: {exc}"
        with self.server.lock:
            self.server.shapes[(len(body.get("prompt", [])), body.get("max_tokens"))] += 1
            failing = self.server.failure_mode
        if error:
            self.send_error(400, error)
            return
        if failing:
            self.send_error(503, "injected CPU fake failure")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for token in range(body["max_tokens"]):
            event = {"choices": [{"text": "x", "token_ids": [token], "finish_reason": None}]}
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.write(
            b"data: "
            + json.dumps(
                {"choices": [{"text": "", "token_ids": [], "finish_reason": "length"}]}
            ).encode()
            + b"\n\n"
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], lengths: tuple[int, ...], model: str) -> None:
        super().__init__(address, _Handler)
        self.lengths = set(lengths)
        self.model = model
        self.shapes: Counter[tuple[int, int]] = Counter()
        self.failure_mode = False
        self.lock = threading.Lock()

    def validate(self, path: str, body: Any) -> str | None:
        if path != "/v1/completions":
            return f"unexpected endpoint {path!r}"
        if body.get("model") != self.model:
            return f"unexpected model {body.get('model')!r}"
        prompt, output_len = body.get("prompt"), body.get("max_tokens")
        if not isinstance(prompt, list) or len(prompt) not in self.lengths:
            return (
                f"unexpected prompt length {len(prompt) if isinstance(prompt, list) else prompt!r}"
            )
        if output_len != len(prompt):
            return "expected equal input/output lengths"
        if body.get("stream") is not True or body.get("temperature") != 0:
            return "expected streaming greedy completions"
        return None


def _invoke(args: argparse.Namespace, server: _FakeServer, path: Path, success: bool) -> None:
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
        "4",
        "--lengths",
        "16,32",
        "--concurrencies",
        "1,2",
        "--vs-output",
        str(path),
    ]
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if (completed.returncode == 0) != success:
        raise RuntimeError(
            f"unexpected exit {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
        )
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if success and records[-1].get("kind") != "result":
        raise RuntimeError(f"matrix did not produce a result: {records[-1]}")
    if not success and (
        records[-1].get("kind") != "error" or "success_steps" not in records[-1].get("message", "")
    ):
        raise RuntimeError(f"RF failures were not surfaced: {records[-1]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", dest="engine", required=True)
    parser.add_argument("--tokenizer", required=True, help="local tokenizer.json for CPU test")
    args = parser.parse_args()
    server = _FakeServer(("127.0.0.1", 0), (16, 32), "cpu-trn2-smoke")
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="llama3-trn2-rf-cpu-") as directory:
            _invoke(args, server, Path(directory) / "success.jsonl", True)
            expected_per_shape = 8
            if server.shapes != {(16, 16): expected_per_shape, (32, 32): expected_per_shape}:
                raise RuntimeError(f"unexpected request-shape matrix: {server.shapes}")
            server.shapes.clear()
            server.failure_mode = True
            _invoke(args, server, Path(directory) / "failure.jsonl", False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("Trainium CPU completions smoke passed: matrix coverage, lengths, metrics, and failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
