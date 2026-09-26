"""Shared CPU-only contract smoke for Request Factory text benchmarks.

Bundle scripts provide only their request matrix and expected contract. This
module owns the fake OpenAI completions server, VibeSys result checks, and
failure-path exercise. It never measures serving performance.
"""

from __future__ import annotations

import argparse
import http.server
import json
import math
import sys
import tempfile
import threading
import tomllib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from tests.support import run_test_command

_ROOT = Path(__file__).resolve().parents[2]
_TOKENIZER = Path(__file__).with_name("fixtures") / "request_factory_tokenizer.json"


class RequestShape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(gt=0)
    output_tokens: int = Field(gt=0)
    count: int = Field(gt=0)


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit: str
    direction: Literal["min", "max"]


class SmokeProfile(BaseModel):
    """Validated bundle-specific inputs for the shared HTTP smoke contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark: str
    model: str
    benchmark_args: tuple[str, ...] = ()
    request_shapes: tuple[RequestShape, ...]
    metrics: Mapping[str, MetricDefinition]
    required_fields: Mapping[str, Any]
    response_style: Literal["usage", "token-chunks"]
    failure_message_contains: str
    unique_prompt_tokens: bool = False

    @property
    def benchmark_path(self) -> Path:
        return (_ROOT / self.benchmark).resolve()

    @property
    def shape_counts(self) -> Counter[tuple[int, int]]:
        counts: Counter[tuple[int, int]] = Counter()
        for shape in self.request_shapes:
            counts[(shape.input_tokens, shape.output_tokens)] += shape.count
        return counts


class _CompletionsHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeServer

    # lint-waiver: LW-031726 [N802]; BaseHTTPRequestHandler dispatches POST to this exact name.
    # > Renaming it would bypass the server's method dispatch; an alias adds needless indirection.
    def do_POST(self) -> None:
        try:
            request_body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if isinstance(request_body, dict):
                body: dict[str, Any] = request_body
                error = self._validate(body)
            else:
                body = {}
                error = "request body must be an object"
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            body = {}
            error = f"invalid JSON request: {exc}"

        with self.server.lock:
            shape = self._shape(body)
            if shape is not None:
                self.server.observed_shapes[shape] += 1
            if error:
                self.server.errors.append(error)
            failure_mode = self.server.failure_mode

        if error:
            self.send_error(400, error)
            return
        if failure_mode:
            self.send_error(503, "injected fake-server failure")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in self._events(body):
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _validate(self, body: Mapping[str, Any]) -> str | None:
        if self.path != "/v1/completions":
            error = f"unexpected endpoint: {self.path}"
        elif body.get("model") != self.server.profile.model:
            error = f"unexpected model: {body.get('model')!r}"
        else:
            prompt = body.get("prompt")
            if not isinstance(prompt, list):
                error = "prompt must be a token-ID list"
            elif any(isinstance(token, bool) or not isinstance(token, int) for token in prompt):
                error = "prompt must contain integer token IDs"
            else:
                error = self._validate_prompt_and_fields(body, prompt)
        return error

    def _validate_prompt_and_fields(self, body: Mapping[str, Any], prompt: list[Any]) -> str | None:
        shape = self._shape(body)
        if shape not in self.server.profile.shape_counts:
            return f"unexpected request shape: {shape!r}"
        if len(prompt) != shape[0]:
            return f"prompt must contain {shape[0]} token IDs"
        if self.server.profile.unique_prompt_tokens and len(set(prompt)) != len(prompt):
            return "prompt token IDs must be unique within each request"
        for key, expected in self.server.profile.required_fields.items():
            if body.get(key) != expected:
                return f"{key} must be {expected!r}"
        return None

    @staticmethod
    def _shape(body: Mapping[str, Any]) -> tuple[int, int] | None:
        prompt = body.get("prompt")
        output_tokens = body.get("max_tokens")
        if not isinstance(prompt, list) or isinstance(output_tokens, bool):
            return None
        if not isinstance(output_tokens, int):
            return None
        return len(prompt), output_tokens

    def _events(self, body: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        prompt = body["prompt"]
        output_tokens = body["max_tokens"]
        if self.server.profile.response_style == "token-chunks":
            events = tuple(
                {"choices": [{"text": "x", "token_ids": [index], "finish_reason": None}]}
                for index in range(output_tokens)
            )
            return (
                *events,
                {"choices": [{"text": "", "token_ids": [], "finish_reason": "length"}]},
            )
        return (
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

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, profile: SmokeProfile) -> None:
        super().__init__(("127.0.0.1", 0), _CompletionsHandler)
        self.profile = profile
        self.failure_mode = False
        self.errors: list[str] = []
        self.observed_shapes: Counter[tuple[int, int]] = Counter()
        self.lock = threading.Lock()


def _run_case(
    profile: SmokeProfile,
    engine: str,
    server: _FakeServer,
    output_path: Path,
    *,
    expect_success: bool,
) -> None:
    command = [
        sys.executable,
        str(profile.benchmark_path),
        "--request-factory-engine",
        engine,
        "--url",
        f"http://127.0.0.1:{server.server_port}",
        "--model",
        profile.model,
        "--tokenizer",
        str(_TOKENIZER),
        *profile.benchmark_args,
        "--vs-output",
        str(output_path),
    ]
    completed = run_test_command(command, check=False, text=True, capture_output=True)
    assert (completed.returncode == 0) == expect_success, (
        f"benchmark exit={completed.returncode}, expected success={expect_success}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert not server.errors, "fake server rejected requests: " + "; ".join(server.errors)
    records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert records, "benchmark did not write any result records"
    assert records[0] == {
        "kind": "hello",
        "protocol": 2,
        "metrics": {name: definition.model_dump() for name, definition in profile.metrics.items()},
    }, f"unexpected VibeSys protocol-v2 hello record: {records[:1]}"
    outcome = records[-1]
    if expect_success:
        values = outcome.get("values", {})
        assert outcome.get("kind") == "result", f"unexpected success record: {outcome}"
        assert set(values) == set(profile.metrics), f"unexpected success metrics: {values}"
        for name, value in values.items():
            assert not isinstance(value, bool), f"metric {name} is not numeric: {value!r}"
            assert isinstance(value, int | float), f"metric {name} is not numeric: {value!r}"
            assert math.isfinite(value), f"metric {name} is invalid: {value!r}"
            assert value >= 0, f"metric {name} is invalid: {value!r}"
    else:
        assert outcome.get("kind") == "error", f"RF failures were not surfaced: {outcome}"
        assert profile.failure_message_contains in outcome.get("message", ""), (
            f"unexpected RF failure message: {outcome}"
        )


def run_cpu_smoke(profile: SmokeProfile, engine: str) -> None:
    """Exercise one bundle's RF request shape, result contract, and failure path."""
    server = _FakeServer(profile)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="request-factory-cpu-smoke-") as directory:
            output = Path(directory) / "success.jsonl"
            _run_case(profile, engine, server, output, expect_success=True)
            assert server.observed_shapes == profile.shape_counts, (
                f"unexpected request-shape counts: {server.observed_shapes}; "
                f"expected {profile.shape_counts}"
            )
            server.observed_shapes.clear()
            server.failure_mode = True
            _run_case(
                profile,
                engine,
                server,
                Path(directory) / "failure.jsonl",
                expect_success=False,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def load_profile(path: Path) -> SmokeProfile:
    """Load and validate one benchmark's smoke contract from its TOML profile."""
    with path.open("rb") as source:
        return SmokeProfile.model_validate(tomllib.load(source))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    assert profile.benchmark_path.is_file(), f"benchmark does not exist: {profile.benchmark_path}"
    run_cpu_smoke(profile, args.request_factory_engine)
    sys.stdout.write(
        "CPU fake-server smoke passed: request shape, metrics, and failure propagation\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
