"""Held-out shared-prefix evaluation API; importing this module never loads MLX.

Public contracts own workload answers, strict streaming validation and metrics.
Only HTTP messages cross into candidate code. Official evaluations own and reap
a fresh server; failures never produce scored partial results.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import math
import os
import random
import re
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "EvaluationError",
    "EvaluationResult",
    "ModelMetadata",
    "Question",
    "RequestObservation",
    "SSEDecoder",
    "StreamCollector",
    "Tokenizer",
    "Workload",
    "WorkloadConfig",
    "evaluate_workload",
    "generate_workload",
    "load_config",
    "resolve_model",
    "run_evaluation",
    "summarize",
    "SavedMeasurement",
    "METRIC_SPECS",
    "ServerEffects",
    "owned_server",
    "exception_details",
    "build_failure_report",
    "GenerationMonitor",
    "ServerGenerationError",
    "generate_evaluation_workloads",
    "evaluate_documents",
    "format_failure_report",
    "LogGenerationMonitor",
]

MODEL_ID = "mlx-community/Llama-3.2-3B-Instruct-4bit"
MODEL_REVISION = "7f0dc925e0d0afb0322d96f9255cfddf2ba5636e"
FACT_KEYS = ("harbor", "archive", "beacon", "orchard")
METRIC_SPECS = {
    "p50_ttft_ms": {"unit": "ms", "direction": "min"},
    "p50_e2e_ms": {"unit": "ms", "direction": "min"},
    "output_throughput_tok_per_sec": {"unit": "tokens/s", "direction": "max"},
    "server_peak_rss_bytes": {"unit": "bytes", "direction": "min"},
    "p50_prompt_processing_ms": {"unit": "ms", "direction": "min", "required": False},
    "metal_peak_memory_bytes": {"unit": "bytes", "direction": "min", "required": False},
}
SYSTEM = (
    "Answer using only the supplied document. Return only the requested code, without explanation."
)
FILLER = (
    "The expedition journal describes ordinary coastal surveys and careful equipment checks. "
    "Teams reviewed tide observations, counted supplies, maintained instruments, and compared "
    "notes before beginning the next survey. These background details contain no verification codes. "
)


class EvaluationError(ValueError):
    """Invalid model, workload, stream, answer or measurement."""


class ServerGenerationError(EvaluationError):
    """The correctness monitor observed an uncaught generation-worker failure."""

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        super().__init__(
            "Uncaught server generation-worker exception; see saved server log evidence"
        )


class GenerationMonitor(Protocol):
    """Wait for fatal worker evidence; deterministic Fakes need no clocks or sleeps."""

    async def wait_for_failure(self) -> dict[str, Any]: ...
    def check(self) -> dict[str, Any] | None: ...


class LogGenerationMonitor:
    """Recognize the installed stock server's uncaught generation-worker log marker."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def wait_for_failure(self) -> dict[str, Any]:
        while True:
            if evidence := self.check():
                return evidence
            await asyncio.sleep(0.1)

    def check(self) -> dict[str, Any] | None:
        tail = _log_tail(self.path)
        if re.search(r"Exception in thread[^\n]*\(_generate\)", tail):
            return {
                "kind": "generation_thread_exception",
                "log_path": str(self.path),
                "stderr_tail": tail,
            }
        return None


def exception_details(error: BaseException) -> dict[str, Any]:
    """Preserve exact exception identity, repr, traceback and chained causes."""
    causes = []
    seen = {id(error)}
    current = error
    while True:
        following = current.__cause__ or current.__context__
        if following is None or id(following) in seen:
            break
        causes.append(
            {
                "type": type(following).__name__,
                "module": type(following).__module__,
                "repr": repr(following),
                "message": str(following),
                "relationship": "cause" if current.__cause__ else "context",
            }
        )
        seen.add(id(following))
        current = following
    return ExceptionRecord.model_validate(
        {
            "type": type(error).__name__,
            "module": type(error).__module__,
            "repr": repr(error),
            "message": str(error),
            "cause_chain": causes,
            "traceback": "".join(traceback.format_exception(error)),
        }
    ).model_dump()


def _log_tail(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 16384))
        return stream.read().decode("utf-8", errors="replace")


def build_failure_report(
    error: BaseException,
    artifact_dir: Path,
    *,
    phase: str = "preparation",
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """Join original client failure and server evidence without claiming a cause."""
    traces_path = artifact_dir / "request-traces.jsonl"
    failed = []
    if traces_path.is_file():
        failed = [
            row
            for line in traces_path.read_text().splitlines()
            if (row := json.loads(line)).get("status") == "failed"
        ]
    initiating = next(
        (row for row in failed if row.get("error_type") == type(error).__name__), None
    )
    context = initiating or (failed[0] if failed else None)
    server_path = artifact_dir / "server.json"
    server = json.loads(server_path.read_text()) if server_path.is_file() else {}
    if server.get("cleanup_error", {}).get("repr") == repr(error):
        phase = "server_cleanup"
        context = None
    return FailureReportRecord.model_validate(
        {
            "exception": exception_details(error),
            "phase": context.get("phase", phase) if context else phase,
            "elapsed_seconds": elapsed_seconds,
            "request_context": context,
            "failed_requests": failed,
            "server": {"metadata": server, "stderr_tail": _log_tail(artifact_dir / "server.log")},
            "monitor_evidence": error.details if isinstance(error, ServerGenerationError) else None,
        }
    ).model_dump()


def format_failure_report(report: dict[str, Any]) -> str:
    """Render exception and relevant server evidence without printing prompts."""
    error = report["exception"]
    context = report.get("request_context") or {}
    metadata = report["server"]["metadata"]
    summary = (
        f"{error['type']} {error['repr']}; phase={report['phase']}; "
        f"document_index={context.get('document_index')}, seed={context.get('document_seed')}, "
        f"question={context.get('question')!r}, key={context.get('key')}; "
        f"elapsed_seconds={report.get('elapsed_seconds')}; "
        f"request_elapsed_seconds={context.get('elapsed_seconds')}, "
        f"stream_done={context.get('stream', {}).get('done')}; "
        f"server_status_before_cleanup={metadata.get('exit_code_before_cleanup')!r}, "
        f"cleanup_requested={metadata.get('cleanup_requested')!r}, "
        f"exit_code={metadata.get('exit_code')!r}"
    )
    tail = "\n".join(report["server"]["stderr_tail"].splitlines()[-24:])
    return summary + (f"\nServer log tail:\n{tail}" if tail else "")


class Contract(BaseModel):
    """External contracts reject misspelled or unrecognized keys."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ExceptionRecord(Contract):
    type: str
    module: str
    repr: str
    message: str
    traceback: str
    cause_chain: list[dict[str, str]]


class FailureReportRecord(Contract):
    exception: ExceptionRecord
    phase: Literal[
        "preparation",
        "configuration",
        "model_resolution",
        "tokenizer_loading",
        "workload_generation",
        "server_startup",
        "http_evaluation",
        "telemetry_validation",
        "server_cleanup",
        "warmup",
        "scored",
    ]
    elapsed_seconds: float | None
    request_context: dict[str, Any] | None
    failed_requests: list[dict[str, Any]]
    server: dict[str, Any]
    monitor_evidence: dict[str, Any] | None


class WorkloadConfig(Contract):
    """The approved immutable operating point and common-prefix length target."""

    target_shared_prefix_tokens: int = Field(default=4096, ge=256, le=8192)
    requests: Literal[4] = 4
    concurrency: Literal[4] = 4
    max_tokens: Literal[64] = 64
    temperature: Literal[0] = 0
    warmup_iterations: Literal[1] = 1
    token_tolerance: int = Field(default=64, ge=1, le=128)


class ModelMetadata(Contract):
    """Pinned identity and precision; no candidate-selected model substitution."""

    model_id: Literal["mlx-community/Llama-3.2-3B-Instruct-4bit"] = MODEL_ID
    revision: Literal["7f0dc925e0d0afb0322d96f9255cfddf2ba5636e"] = MODEL_REVISION
    quantization_bits: Literal[4] = 4
    quantization_group_size: Literal[64] = 64


class Tokenizer(Protocol):
    """The only tokenizer operations used; supports offline deterministic Fakes."""

    def encode(self, text: str, *, add_special_tokens: bool = False) -> list[int]: ...

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> list[int] | Mapping[str, Any]: ...


class Question(Contract):
    key: str
    question: str
    expected: str
    messages: list[dict[str, str]]
    rendered_tokens: list[int] = Field(default_factory=list)


class Workload(Contract):
    seed: int
    document: str
    facts: dict[str, str]
    requests: list[Question]
    warmup: Question
    document_tokens: int
    shared_prefix_tokens: int
    prefix_sha256: str


class RequestObservation(Contract):
    answer: str
    ttft_ms: float
    e2e_ms: float
    completion_tokens: int
    prompt_tokens: int
    cached_tokens: int | None = None
    events: list[dict[str, Any]]
    started_at: float
    ended_at: float


class EvaluationResult(Contract):
    metrics: dict[str, float]
    artifacts: dict[str, str]
    observations: list[RequestObservation]
    workloads: list[Workload]
    diagnostics: dict[str, str]
    server: dict[str, Any]


class SavedMeasurement(Contract):
    """Authoritative benchmark-to-report artifact, including reviewed round identity."""

    schema_version: Literal[1] = 1
    round: int | None = Field(default=None, ge=0, strict=True)
    status: Literal["baseline", "official", "provisional", "unassigned"]
    metrics: dict[str, float]
    diagnostics: dict[str, str]
    artifacts: dict[str, str]

    def model_post_init(self, context: Any) -> None:
        required = {key for key, spec in METRIC_SPECS.items() if spec.get("required", True)}
        if not required.issubset(self.metrics) or set(self.metrics) - set(METRIC_SPECS):
            raise EvaluationError(
                "Saved measurement requires four declared metrics and only known optional metrics"
            )
        if not all(math.isfinite(value) and value >= 0 for value in self.metrics.values()):
            raise EvaluationError("Saved measurement metrics must be finite and nonnegative")
        if self.status == "baseline" and self.round != 0:
            raise EvaluationError("Baseline measurement requires round zero")
        if self.status == "official" and (self.round is None or self.round == 0):
            raise EvaluationError("Official optimized measurement requires positive round")


def load_config(path: Path) -> WorkloadConfig:
    """Validate authoritative workload JSON without importing model libraries."""
    return WorkloadConfig.model_validate_json(path.read_text())


def _question(key: str, facts: dict[str, str], document: str, tokenizer: Tokenizer) -> Question:
    question = f"What is the verification code for {key}?"
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Document:\n{document}\n\nQuestion: {question}"},
    ]
    return Question(
        key=key,
        question=question,
        expected=facts[key],
        messages=messages,
        rendered_tokens=_render_tokens(tokenizer, messages),
    )


def _render_tokens(tokenizer: Tokenizer, messages: list[dict[str, str]]) -> list[int]:
    rendered = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    # Transformers 5 defaults to BatchEncoding, while MLX's wrapper requests
    # return_dict=False. These two documented forms carry identical input IDs.
    tokens = rendered["input_ids"] if isinstance(rendered, Mapping) else rendered
    if not isinstance(tokens, list) or not all(type(token) is int for token in tokens):
        raise EvaluationError("Chat template must produce one integer token sequence")
    return tokens


def _common_prefix(requests: list[Question]) -> list[int]:
    streams = [question.rendered_tokens for question in requests]
    length = 0
    for tokens in zip(*streams, strict=False):
        if len(set(tokens)) != 1:
            break
        length += 1
    return streams[0][:length]


def _document(word_count: int, facts: dict[str, str]) -> str:
    words = FILLER.split()
    blocks = []
    for index, key in enumerate(FACT_KEYS):
        count = word_count // 4 + (index < word_count % 4)
        filler = " ".join(words[position % len(words)] for position in range(count))
        blocks.append(
            f"Survey section {index + 1}: {filler}\nThe {key} verification code is {facts[key]}."
        )
    return "\n\n".join(blocks)


def generate_workload(config: WorkloadConfig, tokenizer: Tokenizer, seed: int) -> Workload:
    """Generate separated fresh facts, measuring the actual rendered token prefix.

    Binary search readable filler length. Chat-template overhead is included,
    and token budgets refer to the common prefix rather than document alone.
    """
    rng = random.Random(seed)
    facts = {key: f"{key}-{rng.getrandbits(40):010x}" for key in FACT_KEYS}
    low, high = 0, config.target_shared_prefix_tokens * 4
    best: tuple[int, str, list[Question], list[int]] | None = None
    while low <= high:
        words = (low + high) // 2
        document = _document(words, facts)
        requests = [_question(key, facts, document, tokenizer) for key in FACT_KEYS]
        prefix = _common_prefix(requests)
        distance = abs(len(prefix) - config.target_shared_prefix_tokens)
        if best is None or distance < best[0]:
            best = (distance, document, requests, prefix)
        if len(prefix) < config.target_shared_prefix_tokens:
            low = words + 1
        else:
            high = words - 1
    if best is None or best[0] > config.token_tolerance:
        raise EvaluationError("Rendered shared prefix cannot meet target token tolerance")
    _, document, requests, prefix = best
    rng.shuffle(requests)
    warmup = _question("harbor", facts, document, tokenizer)
    warmup.question = "Which verification code belongs to harbor?"
    warmup.messages[1]["content"] = f"Document:\n{document}\n\nQuestion: {warmup.question}"
    warmup.rendered_tokens = _render_tokens(tokenizer, warmup.messages)
    return Workload(
        seed=seed,
        document=document,
        facts=facts,
        requests=requests,
        warmup=warmup,
        document_tokens=len(tokenizer.encode(document, add_special_tokens=False)),
        shared_prefix_tokens=len(prefix),
        prefix_sha256=hashlib.sha256(json.dumps(prefix).encode()).hexdigest(),
    )


def generate_evaluation_workloads(
    mode: Literal["correctness", "benchmark"],
    config: WorkloadConfig,
    tokenizer: Tokenizer,
    seed: int,
) -> list[Workload]:
    """Keep benchmark operating point and held-out changed-document order authoritative."""
    if mode not in ("correctness", "benchmark"):
        raise EvaluationError(f"Unknown evaluation mode: {mode}")
    workloads = [generate_workload(config, tokenizer, seed)]
    if mode == "correctness":
        changed = generate_workload(config, tokenizer, seed + 1)
        by_key = {question.key: question for question in changed.requests}
        changed.requests = [by_key[question.key] for question in reversed(workloads[0].requests)]
        workloads.append(changed)
    return workloads


class SSEDecoder:
    """Incremental UTF-8/SSE framing, including arbitrary byte fragmentation.

    Comments and standard SSE fields are accepted. A dangling event or invalid
    encoding fails instead of treating transport truncation as completion.
    """

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("strict")
        self._buffer = ""
        self._lines: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        try:
            self._buffer += self._utf8.decode(chunk)
        except UnicodeDecodeError as error:
            raise EvaluationError("Invalid UTF-8 in SSE stream") from error
        payloads = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.removesuffix("\r")
            if not line:
                if self._lines:
                    payloads.append("\n".join(self._lines))
                    self._lines.clear()
            elif line.startswith("data:"):
                self._lines.append(line[5:].removeprefix(" "))
            elif not line.startswith((":", "event:", "id:", "retry:")):
                raise EvaluationError("Invalid SSE field or non-streaming JSON response")
        return payloads

    def finish(self) -> None:
        try:
            self._buffer += self._utf8.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise EvaluationError("Truncated UTF-8 in SSE stream") from error
        if self._buffer.strip() or self._lines:
            raise EvaluationError("Truncated SSE event")


class _Delta(Contract):
    role: Literal["assistant"] | None = None
    content: str | None = None
    refusal: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    function_call: dict[str, Any] | None = None


class _Choice(Contract):
    index: Literal[0]
    delta: _Delta
    finish_reason: (
        Literal["stop", "length", "tool_calls", "content_filter", "function_call"] | None
    ) = None
    logprobs: dict[str, Any] | None = None


class _TokenDetails(Contract):
    cached_tokens: int | None = Field(default=None, ge=0, strict=True)
    audio_tokens: int | None = Field(default=None, ge=0, strict=True)
    reasoning_tokens: int | None = Field(default=None, ge=0, strict=True)
    accepted_prediction_tokens: int | None = Field(default=None, ge=0, strict=True)
    rejected_prediction_tokens: int | None = Field(default=None, ge=0, strict=True)


class _Usage(Contract):
    prompt_tokens: int = Field(ge=1, strict=True)
    completion_tokens: int = Field(ge=1, le=64, strict=True)
    total_tokens: int = Field(ge=2, strict=True)
    prompt_tokens_details: _TokenDetails | None = None
    completion_tokens_details: _TokenDetails | None = None


class _Chunk(Contract):
    id: str
    object: Literal["chat.completion.chunk", "chat.completion"]
    created: int
    model: str
    choices: list[_Choice]
    usage: _Usage | None = None
    system_fingerprint: str | None = None
    service_tier: str | None = None


class StreamCollector:
    """Validate answer-specific streamed content and measure actual arrival times."""

    def __init__(self, expected: str, clock: Callable[[], float], started_at: float) -> None:
        self._expected = expected
        self._clock = clock
        self._start = started_at
        self._decoder = SSEDecoder()
        self._answer = ""
        self._first: float | None = None
        self._end: float | None = None
        self._finished = False
        self._done = False
        self._usage: _Usage | None = None
        self._events: list[dict[str, Any]] = []
        self._identity: tuple[str, str, int] | None = None

    def feed(self, chunk: bytes) -> None:
        """Consume available bytes immediately, without buffering the whole response."""
        for payload in self._decoder.feed(chunk):
            arrived = self._clock()
            self._events.append({"time_seconds": arrived, "data": payload})
            self._consume(payload, arrived)

    def snapshot(self) -> dict[str, Any]:
        """Retain partial arrival evidence when a stream or answer fails."""
        return {
            "events": list(self._events),
            "answer": self._answer,
            "started_at": self._start,
            "first_content_at": self._first,
            "terminal_at": self._end,
            "finished": self._finished,
            "done": self._done,
        }

    def _consume(self, payload: str, arrived: float) -> None:
        if self._done:
            raise EvaluationError("SSE event after terminal [DONE]")
        if payload == "[DONE]":
            self._done, self._end = True, arrived
            return
        try:
            chunk = _Chunk.model_validate_json(payload)
        except ValidationError as error:
            raise EvaluationError(f"Invalid OpenAI streaming chunk: {error}") from error
        identity = (chunk.id, chunk.model, chunk.created)
        if self._identity is not None and self._identity != identity:
            raise EvaluationError("Stream response identity changed")
        self._identity = identity
        if chunk.object == "chat.completion" and (chunk.choices or chunk.usage is None):
            raise EvaluationError(
                "chat.completion is accepted only for stock MLX usage-only events"
            )
        self._consume_usage(chunk.usage)
        if len(chunk.choices) > 1:
            raise EvaluationError("Expected exactly one streamed choice")
        if not chunk.choices and chunk.usage is None:
            raise EvaluationError("Empty chunk without token usage")
        for choice in chunk.choices:
            self._consume_choice(choice, arrived)

    def _consume_usage(self, usage: _Usage | None) -> None:
        if usage is None:
            return
        if usage.total_tokens != usage.prompt_tokens + usage.completion_tokens:
            raise EvaluationError("Inconsistent token usage total")
        details = usage.prompt_tokens_details
        if (
            details
            and details.cached_tokens is not None
            and details.cached_tokens > usage.prompt_tokens
        ):
            raise EvaluationError("Cached token count exceeds prompt token count")
        if self._usage is not None and self._usage != usage:
            raise EvaluationError("Conflicting streamed usage counts")
        self._usage = usage

    def _consume_choice(self, choice: _Choice, arrived: float) -> None:
        delta = choice.delta
        if delta.tool_calls or delta.function_call or delta.refusal:
            raise EvaluationError("Tool calls or refusal cannot answer sentinel questions")
        if delta.content:
            if self._finished:
                raise EvaluationError("Content after finish_reason")
            if self._first is None:
                self._first = arrived
            self._answer += delta.content
        if choice.finish_reason is not None:
            if self._finished or choice.finish_reason != "stop":
                raise EvaluationError("Invalid, duplicate or non-stop completion termination")
            self._finished = True

    def finish(self) -> RequestObservation:
        """Require answer, successful termination, [DONE] and actual usage counts."""
        self._decoder.finish()
        if not self._done or not self._finished or self._first is None or self._usage is None:
            raise EvaluationError(
                "Incomplete stream: content, finish_reason, usage and [DONE] required"
            )
        if self._answer.strip().strip("`\"'").rstrip(".") != self._expected:
            raise EvaluationError(f"Incorrect answer for corresponding question: {self._answer!r}")
        assert self._end is not None
        ttft, e2e = (self._first - self._start) * 1000, (self._end - self._start) * 1000
        if not all(math.isfinite(value) for value in (ttft, e2e)) or ttft < 0 or e2e < ttft:
            raise EvaluationError("Non-finite or invalid stream timing")
        details = self._usage.prompt_tokens_details
        return RequestObservation(
            answer=self._answer,
            ttft_ms=ttft,
            e2e_ms=e2e,
            completion_tokens=self._usage.completion_tokens,
            prompt_tokens=self._usage.prompt_tokens,
            cached_tokens=details.cached_tokens if details else None,
            events=self._events,
            started_at=self._start,
            ended_at=self._end,
        )


async def _request(
    question: Question,
    client: httpx.AsyncClient,
    config: WorkloadConfig,
    clock: Callable[[], float],
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    *,
    context: dict[str, Any] | None = None,
) -> RequestObservation:
    payload = {
        "model": "default_model",
        "messages": question.messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started_at = clock()
    collector = StreamCollector(question.expected, clock, started_at)
    trace: dict[str, Any] = {
        "key": question.key,
        "question": question.question,
        "expected": question.expected,
        "request": payload,
        **(context or {}),
    }
    try:
        async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
            trace["http_status"] = response.status_code
            trace["content_type"] = response.headers.get("content-type")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "text/event-stream":
                raise EvaluationError(f"Expected text/event-stream, received {content_type!r}")
            async for chunk in response.aiter_bytes():
                collector.feed(chunk)
        result = collector.finish()
        trace["status"] = "passed"
        return result
    except BaseException as error:
        trace.update(
            status="failed",
            error_type=type(error).__name__,
            error=str(error),
            original_exception=exception_details(error),
        )
        raise
    finally:
        if trace_sink is not None:
            trace_sink(
                {**trace, "elapsed_seconds": clock() - started_at, "stream": collector.snapshot()}
            )


async def evaluate_workload(
    workload: Workload,
    client: httpx.AsyncClient,
    config: WorkloadConfig,
    clock: Callable[[], float] = time.perf_counter,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    *,
    monitor: GenerationMonitor | None = None,
    document_index: int = 0,
) -> list[RequestObservation]:
    """Validate one warmup before dispatching all four requests concurrently."""
    if monitor is None:
        return await _evaluate_requests(workload, client, config, clock, trace_sink, document_index)
    if evidence := monitor.check():
        raise ServerGenerationError(evidence)
    operation = asyncio.create_task(
        _evaluate_requests(workload, client, config, clock, trace_sink, document_index)
    )
    watching = asyncio.create_task(monitor.wait_for_failure())
    try:
        done, _ = await asyncio.wait((operation, watching), return_when=asyncio.FIRST_COMPLETED)
        if watching in done:
            raise ServerGenerationError(watching.result())
        if evidence := monitor.check():
            raise ServerGenerationError(evidence)
        return operation.result()
    finally:
        operation.cancel()
        watching.cancel()
        await asyncio.gather(operation, watching, return_exceptions=True)


async def _evaluate_requests(
    workload: Workload,
    client: httpx.AsyncClient,
    config: WorkloadConfig,
    clock: Callable[[], float],
    trace_sink: Callable[[dict[str, Any]], None] | None,
    document_index: int,
) -> list[RequestObservation]:
    context = {"document_seed": workload.seed, "document_index": document_index}
    await _request(
        workload.warmup, client, config, clock, trace_sink, context={**context, "phase": "warmup"}
    )
    tasks = [
        asyncio.create_task(
            _request(
                question, client, config, clock, trace_sink, context={**context, "phase": "scored"}
            )
        )
        for question in workload.requests
    ]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def evaluate_documents(
    workloads: list[Workload],
    client: httpx.AsyncClient,
    config: WorkloadConfig,
    clock: Callable[[], float] = time.perf_counter,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    *,
    monitor: GenerationMonitor | None = None,
) -> list[RequestObservation]:
    """Evaluate all changed documents through one client and server state."""
    observations = []
    for index, workload in enumerate(workloads):
        observations.extend(
            await evaluate_workload(
                workload, client, config, clock, trace_sink, monitor=monitor, document_index=index
            )
        )
    return observations


def summarize(observations: list[RequestObservation]) -> dict[str, float]:
    """Median request timings and usage-token throughput over the scored makespan."""
    if len(observations) != 4:
        raise EvaluationError("Exactly four scored observations required")
    makespan = max(row.ended_at for row in observations) - min(
        row.started_at for row in observations
    )
    if not math.isfinite(makespan) or makespan <= 0:
        raise EvaluationError("Scored makespan must be finite and positive")
    metrics = {
        "p50_ttft_ms": statistics.median(row.ttft_ms for row in observations),
        "p50_e2e_ms": statistics.median(row.e2e_ms for row in observations),
        "output_throughput_tok_per_sec": sum(row.completion_tokens for row in observations)
        / makespan,
    }
    if not all(math.isfinite(value) and value >= 0 for value in metrics.values()):
        raise EvaluationError("All protocol metrics must be finite and nonnegative")
    return metrics


def resolve_model(path: Path | None, metadata: ModelMetadata) -> Path:
    """Validate a local snapshot, its revision provenance and quantization only.

    Resolve explicit paths/symlinks without any Hugging Face network call.
    A copied snapshot requires a revision.txt provenance file.
    """
    if path is None:
        cache = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache/huggingface/hub"))
        path = (
            cache
            / ("models--" + metadata.model_id.replace("/", "--"))
            / "snapshots"
            / metadata.revision
        )
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise EvaluationError(f"Local model directory missing: {path}; downloads are disabled")
    provenance = path.name == metadata.revision or (
        (path / "revision.txt").is_file()
        and (path / "revision.txt").read_text().strip() == metadata.revision
    )
    if not provenance:
        raise EvaluationError(f"Model path lacks pinned revision provenance: {path}")
    config = json.loads((path / "config.json").read_text())
    quantization = config.get("quantization", config.get("quantization_config", {}))
    if (
        quantization.get("bits") != metadata.quantization_bits
        or quantization.get("group_size") != metadata.quantization_group_size
    ):
        raise EvaluationError(f"Model quantization does not match pinned metadata: {path}")
    if not list(path.glob("*.safetensors")) or not (path / "tokenizer.json").is_file():
        raise EvaluationError(f"Cached model is incomplete: {path}")
    return path


def _save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _versions() -> dict[str, str]:
    versions = {}
    for package in ("mlx", "mlx-lm", "transformers", "httpx", "pydantic", "vibesys"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "unavailable"
    return versions


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _server_child(pid_path: Path) -> int | None:
    if not pid_path.is_file():
        return None
    value = pid_path.read_text().strip()
    if not value.isdecimal() or int(value) <= 1:
        raise EvaluationError("Invalid owned server PID record")
    return int(value)


def _reap(process: subprocess.Popen, child: int | None) -> None:
    if process.poll() is not None:
        process.wait()
        return
    if child is not None:
        try:
            if os.getpgid(child) == process.pid:
                os.kill(child, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


class ServerEffects(Protocol):
    """Replace process/network/clock effects for lifecycle contract tests."""

    def launch(
        self, command: list[str], root: Path, environment: dict[str, str], log: Any
    ) -> Any: ...
    def allocate_port(self) -> int: ...
    def child_pid(self, path: Path) -> int | None: ...
    def reap(self, process: Any, child: int | None) -> None: ...
    def ready(self, base_url: str) -> bool: ...
    def clock(self) -> float: ...
    def pause(self, seconds: float) -> None: ...


class _LiveServerEffects:
    def allocate_port(self) -> int:
        return _unused_port()

    def launch(
        self, command: list[str], root: Path, environment: dict[str, str], log: Any
    ) -> subprocess.Popen:
        return subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

    def child_pid(self, path: Path) -> int | None:
        return _server_child(path)

    def reap(self, process: subprocess.Popen, child: int | None) -> None:
        _reap(process, child)

    def ready(self, base_url: str) -> bool:
        try:
            return httpx.get(base_url + "/v1/models", timeout=1, trust_env=False).status_code == 200
        except httpx.TransportError:
            return False

    def clock(self) -> float:
        return time.monotonic()

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)


@contextmanager
def owned_server(
    root: Path,
    model: Path,
    artifacts: Path,
    *,
    effects: ServerEffects | None = None,
) -> Iterator[dict[str, Any]]:
    """External time owns candidate subprocess; wait/reap before reading RSS."""
    if effects is None and sys.platform != "darwin":
        raise EvaluationError("Official peak RSS measurement requires macOS /usr/bin/time -l")
    effects = effects or _LiveServerEffects()
    port = effects.allocate_port()
    resource_path = artifacts / "server-resource.txt"
    pid_path = artifacts / "server.pid"
    # This static trampoline records its PID before exec. The timer remains the
    # parent and can reap the exact candidate process and emit its resource data.
    trampoline = (
        "import os,sys; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "os.execv(sys.executable,[sys.executable,*sys.argv[2:]])"
    )
    command = [
        "/usr/bin/time",
        "-l",
        "-o",
        str(resource_path),
        sys.executable,
        "-c",
        trampoline,
        str(pid_path),
        str(root / "server.py"),
        "--model-path",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    environment = dict(
        os.environ,
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
    )
    record: dict[str, Any] = {
        "command": command,
        "cwd": str(root),
        "environment": {
            key: environment[key]
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
        },
        "base_url": f"http://127.0.0.1:{port}",
        "rss_scope": "server process high-water RSS, startup + warmup + scored requests; excludes Metal allocator/total unified memory",
        "stock_defaults": {
            "decode_concurrency": 32,
            "prompt_concurrency": 8,
            "prefill_step_size": 2048,
            "cache_size": 10,
        },
    }
    with (artifacts / "server.log").open("w") as log:
        process = effects.launch(command, root, environment, log)
        child = None
        try:
            deadline = effects.clock() + 120
            while effects.clock() < deadline:
                if process.poll() is not None:
                    raise EvaluationError(
                        f"Server exited during startup with status {process.returncode}; see server.log"
                    )
                child = child or effects.child_pid(pid_path)
                if child is not None and effects.ready(record["base_url"]):
                    break
                effects.pause(0.2)
            else:
                raise EvaluationError("Server readiness timed out after 120 seconds")
            yield record
        except BaseException as error:
            record["server_status_at_failure"] = {
                "timer_exit_code": process.poll(),
                "candidate_pid": child,
                "exception": exception_details(error),
            }
            raise
        finally:
            if child is None:
                try:
                    child = effects.child_pid(pid_path)
                except EvaluationError as error:
                    record["pid_error"] = str(error)
            record["exit_code_before_cleanup"] = process.poll()
            record["cleanup_requested"] = record["exit_code_before_cleanup"] is None
            try:
                effects.reap(process, child)
            except Exception as error:
                record["cleanup_error"] = exception_details(error)
                _save(artifacts / "server.json", record)
                raise
            record["exit_code"] = process.returncode
            record["exit_cause"] = "cleanup" if record["cleanup_requested"] else "process_exit"
            record["resource_file"] = str(resource_path)
            if resource_path.is_file():
                match = re.search(
                    r"^\s*(\d+)\s+maximum resident set size\s*$",
                    resource_path.read_text(),
                    re.MULTILINE,
                )
                if match:
                    record["server_peak_rss_bytes"] = int(match.group(1))
            _save(artifacts / "server.json", record)


async def _run_workloads(
    workloads: list[Workload],
    base_url: str,
    config: WorkloadConfig,
    artifacts: Path,
    *,
    correctness: bool,
) -> list[RequestObservation]:
    observations = []
    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(120), trust_env=False
    ) as client:
        monitor = LogGenerationMonitor(artifacts / "server.log") if correctness else None

        def save_trace(trace: dict[str, Any]) -> None:
            with (artifacts / "request-traces.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"seed": trace["document_seed"], **trace}, allow_nan=False) + "\n"
                )

        observations = await evaluate_documents(
            workloads, client, config, trace_sink=save_trace, monitor=monitor
        )
    return observations


def run_evaluation(
    mode: Literal["correctness", "benchmark"],
    artifact_dir: Path,
    seed: int | None = None,
    *,
    project_root: Path | None = None,
    model_path: Path | None = None,
) -> EvaluationResult:
    """Run a fresh owned server, retain evidence, and return only valid measurements.

    Correctness uses two changed documents and permuted question orders.
    Artifacts must be new; failure writes diagnostic evidence and propagates.
    """
    if mode not in ("correctness", "benchmark"):
        raise EvaluationError(f"Unknown evaluation mode: {mode}")
    task = Path(__file__).resolve().parent
    root = project_root or task.parents[2]
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=False)
    seed = secrets.randbits(63) if seed is None else seed
    invocation = {
        "argv": sys.argv,
        "cwd": str(Path.cwd()),
        "time_utc": datetime.now(UTC).isoformat(),
        "mode": mode,
        "seed": seed,
        "python": sys.version,
        "executable": sys.executable,
        "versions": _versions(),
        "environment": {
            key: os.environ[key]
            for key in (
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "HF_HUB_CACHE",
                "HF_HOME",
                "MLX_MODEL_PATH",
                "PYTHONDONTWRITEBYTECODE",
            )
            if key in os.environ
        },
    }
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    invocation["candidate_commit"] = revision.stdout.strip() if revision.returncode == 0 else None
    git_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    invocation["git_root"] = git_root.stdout.strip() if git_root.returncode == 0 else None
    invocation["git_status"] = dirty.stdout if dirty.returncode == 0 else None
    sources = [
        root / "server.py",
        task / "evaluator.py",
        task / "workload.json",
        task / "reference/meta.json",
        root / "pyproject.toml",
    ]
    invocation["source_sha256"] = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sources
        if path.is_file()
    }
    _save(artifact_dir / "invocation.json", invocation)
    started_at = time.perf_counter()
    progress = {"phase": "configuration"}
    try:
        return _evaluation(mode, artifact_dir, seed, root, task, model_path, progress)
    except Exception as error:
        report = build_failure_report(
            error,
            artifact_dir,
            phase=progress["phase"],
            elapsed_seconds=time.perf_counter() - started_at,
        )
        _save(
            artifact_dir / "failure.json",
            report,
        )
        raise EvaluationError(
            f"Evaluation failed: {format_failure_report(report)}; artifacts: {artifact_dir}"
        ) from error


def _evaluation(
    mode: str,
    artifacts: Path,
    seed: int,
    root: Path,
    task: Path,
    model_path: Path | None,
    progress: dict[str, str],
) -> EvaluationResult:
    config = load_config(task / "workload.json")
    metadata = ModelMetadata.model_validate_json((task / "reference/meta.json").read_text())
    progress["phase"] = "model_resolution"
    configured = model_path or (
        Path(os.environ["MLX_MODEL_PATH"]) if "MLX_MODEL_PATH" in os.environ else None
    )
    if configured is None and (task / "reference/model").is_dir():
        configured = task / "reference/model"
    model = resolve_model(configured, metadata)
    progress["phase"] = "tokenizer_loading"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        str(model), local_files_only=True, trust_remote_code=False
    )
    if not tokenizer.chat_template:
        raise EvaluationError(
            "Cached tokenizer has no chat template; explicit stock-compatible rendering is required"
        )
    progress["phase"] = "workload_generation"
    workloads = generate_evaluation_workloads(mode, config, tokenizer, seed)
    _save(artifacts / "workloads.json", [workload.model_dump() for workload in workloads])
    _save(artifacts / "model.json", {**metadata.model_dump(), "path": str(model)})
    progress["phase"] = "server_startup"
    with owned_server(root, model, artifacts) as server:
        progress["phase"] = "http_evaluation"
        observations = asyncio.run(
            _run_workloads(
                workloads, server["base_url"], config, artifacts, correctness=mode == "correctness"
            )
        )
        _save(artifacts / "observations.json", [row.model_dump() for row in observations])
    progress["phase"] = "telemetry_validation"
    if "server_peak_rss_bytes" not in server:
        raise EvaluationError(
            "Server high-water RSS unavailable; measurement is not a valid baseline"
        )
    metrics = summarize(observations) if mode == "benchmark" else {}
    if metrics:
        metrics["server_peak_rss_bytes"] = float(server["server_peak_rss_bytes"])
    result = EvaluationResult(
        metrics=metrics,
        artifacts={"directory": str(artifacts)},
        observations=observations,
        workloads=workloads,
        server=server,
        diagnostics={
            "p50_prompt_processing_ms": "Unavailable: stock MLX-LM HTTP does not expose prompt-processing duration",
            "metal_peak_memory_bytes": "Unavailable: stock MLX-LM HTTP does not expose Metal allocator peak",
        },
    )
    _save(artifacts / "evaluation.json", result.model_dump())
    return result
