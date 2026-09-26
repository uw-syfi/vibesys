#!/usr/bin/env python3
"""Validate the RF completions request and result contract against a CPU fake."""

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


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _FakeServer

    def do_POST(self) -> None:  # noqa: N802  # LW-930301; BaseHTTPRequestHandler dispatch requires this method name.
        # > A renamed helper still needs a do_POST wrapper, so indirection would not remove the required spelling.
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            error = self._validate(body)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            body = {}
            error = f"invalid request: {exc}"
        with self.server.lock:
            self.server.count += 1
            if error:
                self.server.errors.append(error)
            failed = self.server.fail_requests
        if error:
            self.send_error(400, error)
            return
        if failed:
            self.send_error(503, "injected fake server error")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        count = body["max_tokens"]
        for event in (
            {"choices": [{"text": "x", "token_ids": list(range(count)), "finish_reason": None}]},
            {
                "choices": [{"text": "", "token_ids": [], "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": len(body["prompt"]),
                    "completion_tokens": count,
                    "total_tokens": len(body["prompt"]) + count,
                },
            },
        ):
            encoded = json.dumps(event).encode()
            self.wfile.write(b"data: " + encoded + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _validate(self, body: Any) -> str | None:
        if self.path != "/v1/completions":
            return f"wrong path: {self.path}"
        if body.get("model") != self.server.model:
            return f"wrong model: {body.get('model')!r}"
        prompt = body.get("prompt")
        if not isinstance(prompt, list) or len(prompt) != self.server.input_tokens:
            return f"prompt must have {self.server.input_tokens} token IDs"
        if any(isinstance(token, bool) or not isinstance(token, int) for token in prompt):
            return "prompt must contain integer token IDs"
        if len(set(prompt)) != len(prompt):
            return "prompt token IDs must not repeat or be truncated"
        if body.get("max_tokens") != self.server.output_tokens:
            return f"max_tokens must be {self.server.output_tokens}"
        if body.get("temperature") != 0 or body.get("stream") is not True:
            return "expected temperature=0 and stream=true"
        if body.get("ignore_eos") is not True or body.get("return_token_ids") is not True:
            return "expected ignore_eos and return_token_ids"
        if body.get("stream_options") != {"include_usage": True}:
            return "expected usage in streaming response"
        return None

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.model = "cpu-smoke-model"
        self.input_tokens = 16
        self.output_tokens = 4
        self.count = 0
        self.fail_requests = False
        self.errors: list[str] = []
        self.lock = threading.Lock()


def _run_case(engine: str, server: _FakeServer, output: Path, expect_success: bool) -> None:
    command = [
        sys.executable,
        str(_BENCHMARK),
        "--request-factory-engine",
        engine,
        "--url",
        f"http://127.0.0.1:{server.server_port}",
        "--model",
        server.model,
        "--tokenizer",
        str(_TOKENIZER),
        "--request-count",
        "8",
        "--input-tokens",
        str(server.input_tokens),
        "--output-tokens",
        str(server.output_tokens),
        "--concurrency",
        "2",
        "--token-pool-limit",
        "16",
        "--text-file",
        str(Path(__file__).with_name("cpu_smoke_corpus.txt")),
        "--vs-output",
        str(output),
    ]
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if (result.returncode == 0) != expect_success:
        raise RuntimeError(
            f"unexpected exit {result.returncode}: {result.stdout}\n{result.stderr}\n"
            f"fake errors: {server.errors}"
        )
    if server.errors:
        raise RuntimeError("fake rejected request: " + "; ".join(server.errors))
    records = [json.loads(line) for line in output.read_text().splitlines()]
    if records[0].get("kind") != "hello" or records[0].get("protocol") != 2:
        raise RuntimeError("missing VibeSys protocol-v2 hello")
    outcome = records[-1]
    if expect_success:
        values = outcome.get("values", {})
        if outcome.get("kind") != "result" or set(values) != {
            "output_token_throughput_per_s",
            "p90_latency_ms",
        }:
            raise RuntimeError(f"unexpected result: {outcome}")
        if any(not isinstance(value, int | float) or value < 0 for value in values.values()):
            raise RuntimeError(f"invalid metrics: {values}")
    elif outcome.get("kind") != "error" or "failed_steps" not in outcome.get("message", ""):
        raise RuntimeError(f"RF failure was not rejected: {outcome}")


def run(engine: str) -> None:
    server = _FakeServer()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="glm-rf-smoke-") as directory:
            output = Path(directory) / "success.jsonl"
            _run_case(engine, server, output, True)
            if server.count != 8:
                raise RuntimeError(f"expected 8 requests, saw {server.count}")
            server.count = 0
            server.fail_requests = True
            _run_case(engine, server, Path(directory) / "failure.jsonl", False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("CPU fake-server validation passed: request encoding, results, and failures")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    run(parser.parse_args().request_factory_engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
