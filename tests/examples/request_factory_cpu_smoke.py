"""Shared CPU-only contract smoke for Request Factory text benchmarks.

Bundle TOML profiles provide request matrices and expected contracts. This
module owns the fake OpenAI completions server, VibeSys result checks, and
failure-path exercise. It never measures serving performance.
"""

from __future__ import annotations

import argparse
import http.server
import json
import sys
import tempfile
import threading
import tomllib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tests.support import run_test_command

from vs_evaluator_protocol.api import Hello, check_objectives, parse_records, read_measurement

_ROOT = Path(__file__).resolve().parents[2]
_TOKENIZER = Path(__file__).with_name("fixtures") / "request_factory_tokenizer.json"
type FailureMode = Literal["http", "malformed-sse", "truncated-sse", "output-mismatch"]


class RequestShape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(gt=0)
    output_tokens: int = Field(gt=0)
    count: int = Field(gt=0)


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit: str
    direction: Literal["min", "max"]


class FailureCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: FailureMode
    message_contains: str = Field(min_length=1)


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
    failure_message_contains: str | None = None
    failure_cases: tuple[FailureCase, ...] = ()
    tokenizer: str | None = None
    corpus_text: str | None = None
    unique_prompt_tokens: bool = False
    unique_prompts: bool = False

    @model_validator(mode="after")
    def _has_failure_contract(self) -> SmokeProfile:
        if self.failure_message_contains is None and not self.failure_cases:
            message = "declare failure_message_contains or failure_cases"
            raise ValueError(message)
        return self

    @property
    def benchmark_path(self) -> Path:
        return (_ROOT / self.benchmark).resolve()

    @property
    def tokenizer_path(self) -> Path:
        if self.tokenizer is None:
            return _TOKENIZER
        return (_ROOT / self.tokenizer).resolve()

    @property
    def shape_counts(self) -> Counter[tuple[int, int]]:
        counts: Counter[tuple[int, int]] = Counter()
        for shape in self.request_shapes:
            counts[(shape.input_tokens, shape.output_tokens)] += shape.count
        return counts

    @property
    def failures(self) -> tuple[FailureCase, ...]:
        if self.failure_cases:
            return self.failure_cases
        assert self.failure_message_contains is not None
        return (FailureCase(mode="http", message_contains=self.failure_message_contains),)


class _CompletionsHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeServer

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

        failure_mode = self.server.record(self._shape(body), body.get("prompt"), error)

        if error:
            self.send_error(400, error)
            return
        if failure_mode == "http":
            self.send_error(503, "injected fake-server failure")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if failure_mode == "malformed-sse":
            self.wfile.write(b"data: {not-json}\n\n")
            self.wfile.flush()
            return
        events = self._events(body, mismatch=failure_mode in {"truncated-sse", "output-mismatch"})
        if failure_mode == "truncated-sse":
            self.wfile.write(b"data: " + json.dumps(events[0]).encode() + b"\n\n")
            self.wfile.flush()
            return
        for event in events:
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

    def _events(self, body: Mapping[str, Any], *, mismatch: bool) -> tuple[Mapping[str, Any], ...]:
        prompt = body["prompt"]
        output_tokens = body["max_tokens"] - int(mismatch)
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

    def log_message(self, format: str, *_args: object) -> None:  # noqa: A002  # LW-031727; preserve stdlib override signature.
        # > Renaming breaks keyword signature compatibility; a generic kwargs signature weakens
        # > type checking, and delegating to the base handler would pollute smoke output.
        del format


class _FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, profile: SmokeProfile) -> None:
        super().__init__(("127.0.0.1", 0), _CompletionsHandler)
        self.profile = profile
        self.failure_mode: FailureMode | None = None
        self.errors: list[str] = []
        self.observed_shapes: Counter[tuple[int, int]] = Counter()
        self.observed_prompts: list[tuple[int, ...]] = []
        self.lock = threading.Lock()

    def record(
        self, shape: tuple[int, int] | None, prompt: object, error: str | None
    ) -> FailureMode | None:
        """Record one request and return its deterministic failure mode."""
        with self.lock:
            if shape is not None:
                self.observed_shapes[shape] += 1
            if isinstance(prompt, list) and all(isinstance(token, int) for token in prompt):
                self.observed_prompts.append(tuple(prompt))
            if error:
                self.errors.append(error)
            return self.failure_mode


def _benchmark_args(profile: SmokeProfile, output_path: Path) -> tuple[str, ...]:
    corpus_path = output_path.parent / "smoke-corpus.txt"
    if profile.corpus_text is not None:
        corpus_path.write_text(profile.corpus_text + "\n", encoding="utf-8")
    args = []
    for argument in profile.benchmark_args:
        if argument == "{corpus}":
            assert profile.corpus_text is not None, "profile uses {corpus} without corpus_text"
            args.append(str(corpus_path))
        else:
            args.append(argument)
    return tuple(args)


def _run_case(
    profile: SmokeProfile,
    engine: str,
    server: _FakeServer,
    output_path: Path,
    *,
    failure: FailureCase | None,
) -> None:
    expect_success = failure is None
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
        str(profile.tokenizer_path),
        *_benchmark_args(profile, output_path),
        "--vs-output",
        str(output_path),
    ]
    completed = run_test_command(command, check=False, text=True, capture_output=True)
    assert (completed.returncode == 0) == expect_success, (
        f"benchmark exit={completed.returncode}, expected success={expect_success}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert not server.errors, "fake server rejected requests: " + "; ".join(server.errors)
    records = parse_records(output_path.read_text(encoding="utf-8"))
    assert records, "benchmark did not write any result records"
    hello = records[0]
    assert isinstance(hello, Hello), f"unexpected first protocol record: {hello}"
    check_objectives(hello, set(profile.metrics))
    assert set(hello.metrics) == set(profile.metrics)
    for name, expected in profile.metrics.items():
        actual = hello.metrics[name]
        assert actual.unit == expected.unit
        assert actual.direction == expected.direction
        assert actual.required
    measurement = read_measurement(records)
    if expect_success:
        assert measurement.failure is None, f"unexpected benchmark failure: {measurement.failure}"
        values = measurement.values
        assert values is not None
        assert set(values) == set(profile.metrics), f"unexpected success metrics: {values}"
        for name, value in values.items():
            assert value >= 0, f"metric {name} is invalid: {value!r}"
    else:
        assert measurement.values is None, f"RF failure produced values: {measurement.values}"
        assert measurement.failure is not None, "RF failure was not surfaced"
        assert failure is not None
        assert failure.message_contains in measurement.failure, (
            f"expected failure containing {failure.message_contains!r}, got {measurement.failure!r}"
        )


def run_cpu_smoke(profile: SmokeProfile, engine: str) -> None:
    """Exercise one bundle's RF request shape, result contract, and failure path."""
    server = _FakeServer(profile)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="request-factory-cpu-smoke-") as directory:
            output = Path(directory) / "success.jsonl"
            _run_case(profile, engine, server, output, failure=None)
            assert server.observed_shapes == profile.shape_counts, (
                f"unexpected request-shape counts: {server.observed_shapes}; "
                f"expected {profile.shape_counts}"
            )
            if profile.unique_prompts:
                assert len(set(server.observed_prompts)) == len(server.observed_prompts), (
                    "Request Factory replayed a prompt within the measured benchmark"
                )
            for index, failure in enumerate(profile.failures):
                server.observed_shapes.clear()
                server.observed_prompts.clear()
                server.failure_mode = failure.mode
                _run_case(
                    profile,
                    engine,
                    server,
                    Path(directory) / f"failure-{index}.jsonl",
                    failure=failure,
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
    assert profile.tokenizer_path.is_file(), f"tokenizer does not exist: {profile.tokenizer_path}"
    run_cpu_smoke(profile, args.request_factory_engine)
    sys.stdout.write(
        "CPU fake-server smoke passed: request shape, metrics, and failure propagation\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
