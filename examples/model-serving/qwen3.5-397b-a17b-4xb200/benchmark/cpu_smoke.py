#!/usr/bin/env python3
"""Validate RF request, result, and failure contracts against a local fake."""

from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_BENCHMARK = Path(__file__).with_name("benchmark.py")
_TOKENIZER = Path(__file__).with_name("cpu_smoke_tokenizer.json")


class _CompletionsHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeServer

    # lint-waiver: LW-019830 [N802]; BaseHTTPRequestHandler dispatches the exact HTTP verb method name, so changing it would break the fake server.
    def do_POST(self) -> None:  # noqa: N802
        request_bytes = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            body = json.loads(request_bytes)
            error = self._validate(body)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            error = f"invalid JSON request: {exc}"
            body = {}

        with self.server.lock:
            self.server.requests += 1
            if error:
                self.server.errors.append(error)
            failure_mode = self.server.failure_mode

        if error:
            self.send_error(400, error)
            return
        if failure_mode:
            self.send_error(503, "injected fake-server failure")
            return

        prompt = body["prompt"]
        output_tokens = body["max_tokens"]
        events = (
            {
                "choices": [
                    {"text": "x", "token_ids": list(range(output_tokens)), "finish_reason": None}
                ]
            },
            {
                "choices": [{"text": "", "token_ids": [], "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": len(prompt),
                    "completion_tokens": output_tokens,
                    "total_tokens": len(prompt) + output_tokens,
                },
            },
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in events:
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _validate(self, body: Any) -> str | None:
        if self.path != "/v1/completions":
            return f"unexpected endpoint: {self.path}"
        if body.get("model") != self.server.expected_model:
            return f"unexpected model: {body.get('model')!r}"
        prompt = body.get("prompt")
        if not isinstance(prompt, list) or len(prompt) != self.server.expected_input_tokens:
            return f"prompt must contain {self.server.expected_input_tokens} token IDs"
        if any(isinstance(token, bool) or not isinstance(token, int) for token in prompt):
            return "prompt must contain integer token IDs"
        if body.get("max_tokens") != self.server.expected_output_tokens:
            return f"max_tokens must be {self.server.expected_output_tokens}"
        if body.get("temperature") != 0 or body.get("stream") is not True:
            return "requests must use temperature=0 and stream=true"
        if body.get("ignore_eos") is not True or body.get("return_token_ids") is not True:
            return "requests must set ignore_eos and return_token_ids"
        if body.get("stream_options") != {"include_usage": True}:
            return "requests must request stream usage"
        return None

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], input_tokens: int, output_tokens: int) -> None:
        super().__init__(address, _CompletionsHandler)
        self.expected_model = "cpu-smoke-model"
        self.expected_input_tokens = input_tokens
        self.expected_output_tokens = output_tokens
        self.failure_mode = False
        self.requests = 0
        self.errors: list[str] = []
        self.lock = threading.Lock()


def _run_case(engine: str, server: _FakeServer, output_path: Path, *, expect_success: bool) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(_BENCHMARK),
            "--request-factory-engine",
            engine,
            "--url",
            f"http://127.0.0.1:{server.server_port}",
            "--model",
            server.expected_model,
            "--tokenizer",
            str(_TOKENIZER),
            "--request-count",
            "8",
            "--input-tokens",
            str(server.expected_input_tokens),
            "--output-tokens",
            str(server.expected_output_tokens),
            "--concurrency",
            "2",
            "--vs-output",
            str(output_path),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if (completed.returncode == 0) != expect_success:
        raise RuntimeError(
            f"benchmark exit={completed.returncode}, expected success={expect_success}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    if server.errors:
        raise RuntimeError("fake server rejected requests: " + "; ".join(server.errors))
    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    if records[0].get("kind") != "hello" or records[0].get("protocol") != 2:
        raise RuntimeError("benchmark did not write the VibeSys protocol-v2 hello record")
    outcome = records[-1]
    if expect_success:
        values = outcome.get("values", {})
        if outcome.get("kind") != "result" or set(values) != {
            "output_token_throughput_per_s",
            "p90_latency_ms",
        }:
            raise RuntimeError(f"unexpected success record: {outcome}")
        if any(not isinstance(value, int | float) or value < 0 for value in values.values()):
            raise RuntimeError(f"invalid metric values: {values}")
    elif outcome.get("kind") != "error" or "failed_steps" not in outcome.get("message", ""):
        raise RuntimeError(f"failed requests were not rejected: {outcome}")


def run(engine: str) -> None:
    server = _FakeServer(("127.0.0.1", 0), input_tokens=16, output_tokens=4)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="qwen397b-rf-smoke-") as directory:
            output = Path(directory) / "success.jsonl"
            _run_case(engine, server, output, expect_success=True)
            if server.requests != 8:
                raise RuntimeError(f"expected 8 requests, observed {server.requests}")
            server.requests = 0
            server.failure_mode = True
            failure = Path(directory) / "failure.jsonl"
            _run_case(engine, server, failure, expect_success=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("CPU fake-server smoke passed: request shape, metrics, and failure propagation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    args = parser.parse_args()
    run(args.request_factory_engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
