"""Metal-free contract properties for the held-out streaming evaluator."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.benchmark import main as benchmark_main
from evaluator import (
    METRIC_SPECS,
    EvaluationError,
    EvaluationResult,
    ModelMetadata,
    SavedMeasurement,
    SSEDecoder,
    StreamCollector,
    WorkloadConfig,
    evaluate_workload,
    generate_workload,
    owned_server,
    resolve_model,
    summarize,
)
from reporting.report import AgentMetadata, ReportRow, render_report

from vs_evaluator_protocol.api import Result, parse_records, read_measurement


class CharacterTokenizer:
    """Deterministic tokenizer preserving chat-prefix boundaries."""

    def encode(self, text: str, *, add_special_tokens: bool = False) -> list[int]:
        return [ord(char) for char in text]

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool = True,
        add_generation_prompt: bool = True,
    ) -> list[int] | str:
        text = "".join(f"<{item['role']}>\n{item['content']}\n" for item in messages)
        if add_generation_prompt:
            text += "<assistant>\n"
        return self.encode(text) if tokenize else text


class LogicalClock:
    """Advance a logical clock on observation, without real time."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


def event(payload: dict[str, Any] | str) -> bytes:
    if isinstance(payload, dict) and "choices" in payload:
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "mlx-community/Llama-3.2-3B-Instruct-4bit",
            **payload,
        }
    value = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {value}\n\n".encode()


def completion(answer: str, *, completion_tokens: int = 8) -> bytes:
    return b"".join(
        [
            event(
                {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
            ),
            event({"choices": [{"index": 0, "delta": {"content": answer}, "finish_reason": None}]}),
            event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            event(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 4096,
                        "completion_tokens": completion_tokens,
                        "total_tokens": 4096 + completion_tokens,
                    },
                }
            ),
            event("[DONE]"),
        ]
    )


def fragments(data: bytes, widths: list[int]) -> Iterator[bytes]:
    start = 0
    for width in widths:
        if start >= len(data):
            break
        yield data[start : start + width]
        start += width
    if start < len(data):
        yield data[start:]


def observe(data: bytes, expected: str = "cedar-1234567890") -> Any:
    clock = LogicalClock()
    collector = StreamCollector(expected=expected, clock=clock, started_at=clock())
    collector.feed(data)
    return collector.finish()


@settings(max_examples=24, deadline=None, derandomize=True)
@given(seed=st.integers(min_value=0, max_value=2**63 - 2))
def test_workload_reproducibility_and_shared_prefix(seed: int) -> None:
    tokenizer = CharacterTokenizer()
    config = WorkloadConfig()
    workload = generate_workload(config, tokenizer, seed)
    replay = generate_workload(config, tokenizer, seed)
    changed = generate_workload(config, tokenizer, seed + 1)
    assert workload == replay
    assert workload.facts != changed.facts
    assert all(workload.facts[key] != changed.facts[key] for key in workload.facts)
    assert len(workload.requests) == 4
    assert len(set(workload.facts.values())) == 4
    assert workload.warmup.question not in {question.question for question in workload.requests}
    assert (
        abs(workload.shared_prefix_tokens - config.target_shared_prefix_tokens)
        <= config.token_tolerance
    )
    rendered = [tokenizer.apply_chat_template(question.messages) for question in workload.requests]
    prefixes = [tokens[: workload.shared_prefix_tokens] for tokens in rendered]
    assert all(prefix == prefixes[0] for prefix in prefixes)
    assert len({tuple(tokens) for tokens in rendered}) == 4
    assert all(question.expected == workload.facts[question.key] for question in workload.requests)
    assert all(value in workload.document for value in workload.facts.values())
    assert workload.prefix_sha256 != changed.prefix_sha256


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requests", 5),
        ("concurrency", 1),
        ("max_tokens", 65),
        ("temperature", 0.1),
        ("warmup_iterations", 0),
        ("unknown_key", True),
    ],
)
def test_immutable_workload_semantics(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        WorkloadConfig(**{field: value})


@settings(max_examples=32, deadline=None, derandomize=True)
@given(widths=st.lists(st.integers(min_value=1, max_value=37), min_size=1, max_size=100))
def test_fragmented_sse_preserves_utf8_and_event_boundaries(widths: list[int]) -> None:
    payloads = ['{"text":"café 🦉"}', "[DONE]"]
    data = b": heartbeat\r\n\r\n" + b"".join(
        event(value).replace(b"\n", b"\r\n") for value in payloads
    )
    decoder = SSEDecoder()
    observed = []
    for part in fragments(data, widths):
        observed.extend(decoder.feed(part))
    decoder.finish()
    assert observed == payloads


@settings(max_examples=32, deadline=None, derandomize=True)
@given(widths=st.lists(st.integers(min_value=1, max_value=83), min_size=1, max_size=100))
def test_stream_collector_fragmentation_keeps_answer_and_usage(widths: list[int]) -> None:
    clock = LogicalClock()
    collector = StreamCollector("cedar-1234567890", clock, clock())
    for part in fragments(completion("cedar-1234567890"), widths):
        collector.feed(part)
    observation = collector.finish()
    assert observation.answer == "cedar-1234567890"
    assert observation.completion_tokens == 8
    assert observation.prompt_tokens == 4096
    assert 0 <= observation.ttft_ms <= observation.e2e_ms
    assert observation.ended_at >= observation.started_at
    assert all(math.isfinite(value) for value in summarize([observation] * 4).values())


def test_stock_mlx_usage_chunk_object_is_accepted() -> None:
    data = completion("cedar-1234567890")
    payloads = data.decode().split("\n\n")
    payloads[-3] = payloads[-3].replace("chat.completion.chunk", "chat.completion")
    observation = observe("\n\n".join(payloads).encode())
    assert observation.completion_tokens == 8


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_protocol_metrics_fail(value: float) -> None:
    observation = observe(completion("cedar-1234567890"))
    corrupted = observation.model_copy(update={"ttft_ms": value})
    with pytest.raises(EvaluationError):
        summarize([corrupted] * 4)


@settings(max_examples=24, deadline=None, derandomize=True)
@given(
    values=st.lists(
        st.floats(min_value=0, max_value=1e15, allow_nan=False, allow_infinity=False),
        min_size=len(METRIC_SPECS),
        max_size=len(METRIC_SPECS),
    )
)
def test_saved_measurement_roundtrip(values: list[float]) -> None:
    row = SavedMeasurement(
        round=0,
        status="baseline",
        metrics=dict(zip(METRIC_SPECS, values, strict=True)),
        diagnostics={},
        artifacts={},
    )
    assert SavedMeasurement.model_validate_json(row.model_dump_json()) == row


@pytest.mark.parametrize(
    "failure",
    ["nan", "infinite", "negative", "missing", "extra", "baseline-round", "official-round"],
)
def test_saved_measurement_rejects_invalid_protocol_rows(failure: str) -> None:
    metrics = dict.fromkeys(METRIC_SPECS, 1.0)
    field = next(iter(metrics))
    round_number, status = 0, "baseline"
    if failure == "nan":
        metrics[field] = float("nan")
    elif failure == "infinite":
        metrics[field] = float("inf")
    elif failure == "negative":
        metrics[field] = -1
    elif failure == "missing":
        del metrics[field]
    elif failure == "extra":
        metrics["unexpected_metric"] = 1
    elif failure == "baseline-round":
        round_number = 1
    else:
        status = "official"
    with pytest.raises((EvaluationError, ValidationError)):
        SavedMeasurement(
            round=round_number, status=status, metrics=metrics, diagnostics={}, artifacts={}
        )


@pytest.mark.parametrize(
    "data",
    [
        b'{"choices": [{"message": {"content": "cedar-1234567890"}}]}',
        b"data: {malformed}\n\n",
        completion("cedar-1234567890").removesuffix(event("[DONE]")),
        completion("cedar-1234567890") + event({"choices": []}),
        completion("cedar-1234567890").replace(
            b'"finish_reason": "stop"', b'"finish_reason": null'
        ),
        completion("cedar-1234567890").replace(
            b'"completion_tokens": 8', b'"completion_tokens": -1'
        ),
        completion("cedar-1234567890").replace(
            b'"completion_tokens": 8', b'"completion_tokens": 1.5'
        ),
        completion("cedar-1234567890").replace(b'"total_tokens": 4104', b'"total_tokens": 99'),
        completion("cedar-1234567890", completion_tokens=65),
        completion("birch-1234567890"),
        completion(""),
    ],
    ids=[
        "plain-json",
        "malformed-json",
        "missing-done",
        "after-done",
        "missing-finish",
        "negative-usage",
        "fractional-usage",
        "inconsistent-usage",
        "over-budget",
        "wrong-answer",
        "empty",
    ],
)
def test_invalid_streams_fail_closed(data: bytes) -> None:
    with pytest.raises(EvaluationError):
        observe(data)


@pytest.mark.parametrize("data", [b"data: incomplete", b"data: \xff\n\n"])
def test_invalid_sse_framing_fails(data: bytes) -> None:
    with pytest.raises(EvaluationError):
        decoder = SSEDecoder()
        decoder.feed(data)
        decoder.finish()


@pytest.mark.parametrize("replacement", [b"true", b'"8"', b"8.0"])
def test_usage_requires_json_integers(replacement: bytes) -> None:
    data = completion("cedar-1234567890").replace(
        b'"completion_tokens": 8', b'"completion_tokens": ' + replacement
    )
    with pytest.raises(EvaluationError):
        observe(data)


@pytest.mark.parametrize("seed", [7, 938])
def test_swapped_and_stale_answers_are_rejected(seed: int) -> None:
    config = WorkloadConfig()
    tokenizer = CharacterTokenizer()
    current = generate_workload(config, tokenizer, seed)
    previous = generate_workload(config, tokenizer, seed + 1)
    for question, swapped in zip(
        current.requests, current.requests[1:] + current.requests[:1], strict=True
    ):
        with pytest.raises(EvaluationError):
            observe(completion(swapped.expected), question.expected)
        with pytest.raises(EvaluationError):
            observe(completion(previous.facts[question.key]), question.expected)


class FragmentedResponse(httpx.AsyncByteStream):
    """A faithful finite response stream with arbitrary HTTP chunk boundaries."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in fragments(self.body, [1, 3, 17, 31]):
            yield part


class InMemoryServingEndpoint(httpx.AsyncBaseTransport):
    """Serve known document-question pairs, with deterministic failure knobs."""

    def __init__(
        self,
        workload: Any,
        *,
        warmup_failure: bool = False,
        content_type: str = "text/event-stream",
    ) -> None:
        self.answers = {
            json.dumps(question.messages, sort_keys=True): question.expected
            for question in [workload.warmup, *workload.requests]
        }
        self.warmup_messages = json.dumps(workload.warmup.messages, sort_keys=True)
        self.warmup_failure = warmup_failure
        self.content_type = content_type
        self.scored_requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["stream"] is True
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 64
        assert payload["stream_options"]["include_usage"] is True
        key = json.dumps(payload["messages"], sort_keys=True)
        answer = self.answers[key]
        if key == self.warmup_messages:
            if self.warmup_failure:
                answer = "wrong-warmup-value"
        else:
            self.scored_requests += 1
        return httpx.Response(
            200,
            headers={"content-type": f"{self.content_type}; charset=utf-8"},
            stream=FragmentedResponse(completion(answer)),
        )


def test_http_evaluation_validates_warmup_and_all_questions() -> None:
    config = WorkloadConfig()
    workload = generate_workload(config, CharacterTokenizer(), 91)
    endpoint = InMemoryServingEndpoint(workload)

    async def run() -> Any:
        async with httpx.AsyncClient(transport=endpoint, base_url="http://fake.invalid") as client:
            return await evaluate_workload(workload, client, config, LogicalClock())

    observations = asyncio.run(run())
    assert len(observations) == 4
    assert {observation.answer for observation in observations} == set(workload.facts.values())
    assert endpoint.scored_requests == 4


def test_failed_warmup_prevents_scored_benchmark() -> None:
    config = WorkloadConfig()
    workload = generate_workload(config, CharacterTokenizer(), 91)
    endpoint = InMemoryServingEndpoint(workload, warmup_failure=True)

    async def run() -> Any:
        async with httpx.AsyncClient(transport=endpoint, base_url="http://fake.invalid") as client:
            return await evaluate_workload(workload, client, config, LogicalClock())

    with pytest.raises(EvaluationError):
        asyncio.run(run())
    assert endpoint.scored_requests == 0


def test_nonstreaming_http_response_is_rejected() -> None:
    config = WorkloadConfig()
    workload = generate_workload(config, CharacterTokenizer(), 91)
    endpoint = InMemoryServingEndpoint(workload, content_type="application/json")

    async def run() -> Any:
        async with httpx.AsyncClient(transport=endpoint, base_url="http://fake.invalid") as client:
            return await evaluate_workload(workload, client, config, LogicalClock())

    with pytest.raises(EvaluationError):
        asyncio.run(run())
    assert endpoint.scored_requests == 0


def cached_snapshot(root: Path) -> Path:
    metadata = ModelMetadata()
    snapshot = root / metadata.revision
    snapshot.mkdir()
    (snapshot / "config.json").write_text(
        json.dumps({"quantization": {"bits": 4, "group_size": 64}})
    )
    (snapshot / "tokenizer_config.json").write_text("{}")
    (snapshot / "tokenizer.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"test fixture, never loaded")
    return snapshot


def test_model_resolution_accepts_pinned_local_snapshot_without_loading_it(tmp_path: Path) -> None:
    snapshot = cached_snapshot(tmp_path)
    assert resolve_model(snapshot, ModelMetadata()) == snapshot
    link = tmp_path / "model"
    link.symlink_to(snapshot, target_is_directory=True)
    assert resolve_model(link, ModelMetadata()) == snapshot


@pytest.mark.parametrize("failure", ["missing", "revision", "quantization", "weights", "tokenizer"])
def test_model_resolution_rejects_incomplete_or_different_models(
    tmp_path: Path, failure: str
) -> None:
    snapshot = cached_snapshot(tmp_path)
    if failure == "missing":
        snapshot = tmp_path / "missing"
    elif failure == "revision":
        changed = tmp_path / "unpinned"
        snapshot.rename(changed)
        snapshot = changed
    elif failure == "quantization":
        (snapshot / "config.json").write_text(
            json.dumps({"quantization": {"bits": 8, "group_size": 64}})
        )
    elif failure == "weights":
        (snapshot / "model.safetensors").unlink()
    else:
        (snapshot / "tokenizer.json").unlink()
    with pytest.raises(EvaluationError):
        resolve_model(snapshot, ModelMetadata())


class InMemoryEvaluationRunner:
    """Evaluate the actual workload against in-memory HTTP with logical time."""

    def __init__(self, *, corrupt_metric: Any = None) -> None:
        self.corrupt_metric = corrupt_metric

    def __call__(
        self, mode: str, artifact_dir: Path, seed: int | None, *, model_path: Path | None = None
    ) -> EvaluationResult:
        assert mode == "benchmark"
        config = WorkloadConfig()
        workload = generate_workload(config, CharacterTokenizer(), 19 if seed is None else seed)
        endpoint = InMemoryServingEndpoint(workload)

        async def run() -> Any:
            async with httpx.AsyncClient(
                transport=endpoint, base_url="http://fake.invalid"
            ) as client:
                return await evaluate_workload(workload, client, config, LogicalClock())

        observations = asyncio.run(run())
        metrics = summarize(observations)
        metrics["server_peak_rss_bytes"] = 8192.0
        if self.corrupt_metric is not None:
            metrics["p50_ttft_ms"] = self.corrupt_metric
        artifact_dir.mkdir(parents=True, exist_ok=False)
        return EvaluationResult(
            metrics=metrics,
            observations=observations,
            workloads=[workload],
            diagnostics={"metal_peak_memory_bytes": "not supplied by stock HTTP"},
            artifacts={},
            server={"kind": "in-memory test fixture"},
        )


def test_benchmark_protocol_and_report_preserve_missing_rounds(tmp_path: Path) -> None:
    artifacts = tmp_path / "fake-evaluation"
    protocol = tmp_path / "result.jsonl"
    assert (
        benchmark_main(
            [
                "--artifact-dir",
                str(artifacts),
                "--vs-output",
                str(protocol),
                "--round",
                "0",
                "--status",
                "baseline",
            ],
            runner=InMemoryEvaluationRunner(),
        )
        == 0
    )
    records = list(parse_records(protocol.read_text()))
    result = next(record for record in records if isinstance(record, Result))
    assert result.label == ""
    measurement = read_measurement(records)
    assert not measurement.failed
    assert measurement.values is not None
    assert "metal_peak_memory_bytes" not in measurement.values
    assert "p50_prompt_processing_ms" not in measurement.values
    row = SavedMeasurement.model_validate_json((artifacts / "measurement.json").read_text())
    destination = tmp_path / "report"
    render_report(
        [
            ReportRow(round=0, status="baseline", source="in-memory fixture", metrics=row.metrics),
            ReportRow(round=2, status="failed", source="failed fixture"),
        ],
        destination,
        {"agent": AgentMetadata().model_dump(), "framework": {"availability": "not requested"}},
    )
    with (destination / "rounds.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["round"], row["status"]) for row in rows] == [
        ("0", "baseline"),
        ("1", "missing"),
        ("2", "failed"),
    ]
    assert all(row["p50_ttft_ms"] == "" for row in rows[1:])
    assert "×" in (destination / "performance.svg").read_text()
    accounting = json.loads((destination / "report-metadata.json").read_text())["agent"]
    assert accounting["input_tokens"] is None
    assert accounting["agent_wall_seconds"] is None


@pytest.mark.parametrize("value", [True, "8", float("nan"), float("inf"), -1.0])
def test_benchmark_rejects_invalid_metrics_and_publishes_protocol_error(
    tmp_path: Path, value: Any
) -> None:
    protocol = tmp_path / "result.jsonl"
    assert (
        benchmark_main(
            ["--artifact-dir", str(tmp_path / "evaluation"), "--vs-output", str(protocol)],
            runner=InMemoryEvaluationRunner(corrupt_metric=value),
        )
        == 1
    )
    measurement = read_measurement(parse_records(protocol.read_text()))
    assert measurement.failed
    assert measurement.values is None


@pytest.mark.parametrize("value", [True, "8", float("nan"), float("inf"), -1.0])
def test_report_rejects_invalid_metric_values_before_writing(tmp_path: Path, value: Any) -> None:
    destination = tmp_path / "report"
    with pytest.raises((ValidationError, ValueError)):
        row = ReportRow(
            round=0, status="baseline", source="fixture", metrics={"p50_ttft_ms": value}
        )
        render_report(
            [row],
            destination,
            {"agent": AgentMetadata().model_dump(), "framework": {"availability": "not requested"}},
        )
    assert not destination.exists()


class MappingTokenizer(CharacterTokenizer):
    """Model Transformers' tokenized BatchEncoding return contract."""

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool = True,
        add_generation_prompt: bool = True,
    ) -> dict[str, list[int]]:
        tokens = super().apply_chat_template(
            messages, tokenize=tokenize, add_generation_prompt=add_generation_prompt
        )
        return {"input_ids": tokens, "attention_mask": [1] * len(tokens)}


def test_workload_supports_transformers_tokenizer_mapping() -> None:
    config = WorkloadConfig()
    assert generate_workload(config, MappingTokenizer(), 918) == generate_workload(
        config, CharacterTokenizer(), 918
    )


class InMemoryServerProcess:
    """Represent candidate lifetime without invoking a subprocess."""

    def __init__(self, exit_code: int | None = None) -> None:
        self.returncode = exit_code

    def poll(self) -> int | None:
        return self.returncode


class InMemoryServerEffects:
    """Own a simulated child, readiness, resource accounting and logical clock."""

    def __init__(self, *, startup_exit: bool = False, ready: bool = True) -> None:
        self.process = InMemoryServerProcess(3 if startup_exit else None)
        self.is_ready = ready
        self.value = 0.0
        self.reaped = False
        self.resource: Path | None = None

    def allocate_port(self) -> int:
        return 18765

    def launch(
        self, command: list[str], root: Path, environment: dict[str, str], log: Any
    ) -> InMemoryServerProcess:
        assert environment["HF_HUB_OFFLINE"] == "1"
        assert environment["TRANSFORMERS_OFFLINE"] == "1"
        self.resource = Path(command[command.index("-o") + 1])
        return self.process

    def child_pid(self, path: Path) -> int:
        return 4321

    def reap(self, process: InMemoryServerProcess, child: int | None) -> None:
        assert child == 4321
        self.reaped = True
        process.returncode = process.returncode if process.returncode is not None else -15
        assert self.resource is not None
        self.resource.write_text(" 8192  maximum resident set size\n")

    def ready(self, base_url: str) -> bool:
        assert base_url == "http://127.0.0.1:18765"
        return self.is_ready

    def clock(self) -> float:
        return self.value

    def pause(self, seconds: float) -> None:
        self.value += 121.0


@pytest.mark.parametrize("failure", [None, "body", "startup-exit", "readiness"])
def test_owned_server_reaps_candidate_and_records_memory_on_all_paths(
    tmp_path: Path, failure: str | None
) -> None:
    effects = InMemoryServerEffects(
        startup_exit=failure == "startup-exit", ready=failure != "readiness"
    )

    def use() -> None:
        with owned_server(tmp_path, tmp_path / "model", tmp_path, effects=effects) as record:
            assert record["base_url"] == "http://127.0.0.1:18765"
            if failure == "body":
                raise EvaluationError("deliberate evaluation failure")

    if failure:
        with pytest.raises(EvaluationError):
            use()
    else:
        use()
    assert effects.reaped
    server = json.loads((tmp_path / "server.json").read_text())
    assert server["server_peak_rss_bytes"] == 8192
    assert server["exit_code"] is not None


class PartialTimeoutResponse(httpx.AsyncByteStream):
    """Deliver partial content, then raise a deterministic transport timeout."""

    def __init__(self, answer: str, message: str) -> None:
        self.answer = answer
        self.message = message

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield event(
            {
                "choices": [
                    {"index": 0, "delta": {"content": self.answer[:4]}, "finish_reason": None}
                ]
            }
        )
        raise httpx.ReadTimeout(self.message)


class TimeoutServingEndpoint(InMemoryServingEndpoint):
    """Simulate a failed HTTP stream while other document questions remain valid."""

    def __init__(self, workload: Any, *, phase: str = "scored", message: str = "") -> None:
        super().__init__(workload)
        self.failure_question = workload.warmup if phase == "warmup" else workload.requests[0]
        self.failure_messages = json.dumps(self.failure_question.messages, sort_keys=True)
        self.message = message

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if json.dumps(payload["messages"], sort_keys=True) == self.failure_messages:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=PartialTimeoutResponse(self.failure_question.expected, self.message),
            )
        return await super().handle_async_request(request)


@pytest.mark.parametrize("phase", ["warmup", "scored"])
def test_blank_read_timeout_preserves_request_and_partial_stream_context(phase: str) -> None:
    config = WorkloadConfig()
    workload = generate_workload(config, CharacterTokenizer(), 938)
    endpoint = TimeoutServingEndpoint(workload, phase=phase)
    traces: list[dict[str, Any]] = []

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=endpoint, base_url="http://fake.invalid", timeout=httpx.Timeout(120)
        ) as client:
            await evaluate_workload(workload, client, config, LogicalClock(), traces.append)

    with pytest.raises(httpx.ReadTimeout) as caught:
        asyncio.run(run())
    assert repr(caught.value) == "ReadTimeout('')"
    failed = next(trace for trace in traces if trace.get("error_type") == "ReadTimeout")
    assert failed["phase"] == phase
    assert failed["document_seed"] == workload.seed
    assert failed["key"] == endpoint.failure_question.key
    assert failed["question"] == endpoint.failure_question.question
    assert failed["expected"] == endpoint.failure_question.expected
    assert failed["http_status"] == 200
    assert failed["elapsed_seconds"] > 0
    assert failed["original_exception"]["type"] == "ReadTimeout"
    assert failed["original_exception"]["repr"] == "ReadTimeout('')"
    assert failed["original_exception"]["message"] == ""
    assert failed["stream"]["answer"] == endpoint.failure_question.expected[:4]
    assert failed["stream"]["done"] is False
    assert failed["stream"]["events"]


@settings(max_examples=20, deadline=None, derandomize=True)
@given(message=st.text(max_size=100))
def test_transport_exception_diagnostic_preserves_arbitrary_messages(message: str) -> None:
    from evaluator import exception_details

    error = httpx.ReadTimeout(message)
    detail = exception_details(error)
    assert detail["type"] == "ReadTimeout"
    assert detail["module"] == "httpx"
    assert detail["repr"] == repr(error)
    assert detail["message"] == message


def test_blank_timeout_report_preserves_original_client_error_and_server_oom(
    tmp_path: Path,
) -> None:
    from evaluator import build_failure_report, format_failure_report

    original = httpx.ReadTimeout("")
    request = {
        "phase": "scored",
        "document_seed": 938,
        "document_index": 1,
        "key": "harbor",
        "question": "Which code?",
        "expected": "harbor-1234567890",
        "error_type": "ReadTimeout",
        "status": "failed",
        "stream": {"done": False, "answer": ""},
    }
    (tmp_path / "request-traces.jsonl").write_text(json.dumps(request) + "\n")
    oom = "RuntimeError: [METAL] Command buffer execution failed: Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)."
    (tmp_path / "server.log").write_text(oom + "\n")
    (tmp_path / "server.json").write_text(
        json.dumps({"exit_code": -15, "exit_code_before_cleanup": None, "cleanup_requested": True})
    )
    report = build_failure_report(original, tmp_path)
    assert report["exception"]["type"] == "ReadTimeout"
    assert report["exception"]["repr"] == "ReadTimeout('')"
    assert report["exception"]["message"] == ""
    assert report["phase"] == "scored"
    assert report["request_context"]["document_index"] == 1
    assert report["request_context"]["stream"]["done"] is False
    assert oom in report["server"]["stderr_tail"]
    assert report["server"]["metadata"]["exit_code_before_cleanup"] is None
    assert report["server"]["metadata"]["cleanup_requested"] is True
    summary = format_failure_report(report)
    assert "ReadTimeout('')" in summary
    assert "kIOGPUCommandBufferCallbackErrorOutOfMemory" in summary
    assert "scored" in summary
    assert "938" in summary


def test_prompt_cache_cap_command_preserves_model_and_workload_constraints(tmp_path: Path) -> None:
    import importlib.util

    candidate = Path(__file__).resolve().parents[4] / "server.py"
    spec = importlib.util.spec_from_file_location("candidate_server_contract", candidate)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = module.build_server_command(tmp_path / "model", "127.0.0.1", 18765)
    assert module.PROMPT_CACHE_SIZE == 1
    assert command[command.index("--prompt-cache-size") + 1] == "1"
    assert command[command.index("--model") + 1] == str(tmp_path / "model")
    assert command[command.index("--max-tokens") + 1] == "64"
    assert command[command.index("--temp") + 1] == "0"
    assert WorkloadConfig().target_shared_prefix_tokens == 4096
    assert WorkloadConfig().concurrency == 4
    assert WorkloadConfig().warmup_iterations == 1


class MultiDocumentServingEndpoint(InMemoryServingEndpoint):
    """Retain one endpoint while answering fresh facts and both distinct warmups."""

    def __init__(self, workloads: list[Any]) -> None:
        super().__init__(workloads[0])
        self.warmups = {
            json.dumps(workload.warmup.messages, sort_keys=True) for workload in workloads
        }
        self.answers = {
            json.dumps(question.messages, sort_keys=True): question.expected
            for workload in workloads
            for question in [workload.warmup, *workload.requests]
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        key = json.dumps(payload["messages"], sort_keys=True)
        if key in self.warmups:
            self.warmup_messages = key
        return await super().handle_async_request(request)


def test_correctness_checks_two_changed_documents_through_same_endpoint(tmp_path: Path) -> None:
    from evaluator import evaluate_documents, generate_evaluation_workloads

    config = WorkloadConfig()
    workloads = generate_evaluation_workloads("correctness", config, CharacterTokenizer(), 938)
    assert len(workloads) == 2
    assert [question.key for question in workloads[1].requests] == [
        question.key for question in reversed(workloads[0].requests)
    ]
    assert set(workloads[0].facts.values()).isdisjoint(workloads[1].facts.values())
    assert len(generate_evaluation_workloads("benchmark", config, CharacterTokenizer(), 938)) == 1
    endpoint = MultiDocumentServingEndpoint(workloads)
    effects = InMemoryServerEffects()
    traces: list[dict[str, Any]] = []

    async def run() -> list[Any]:
        with owned_server(tmp_path, tmp_path / "model", tmp_path, effects=effects) as server:
            async with httpx.AsyncClient(
                transport=endpoint, base_url=server["base_url"], timeout=httpx.Timeout(120)
            ) as client:
                return await evaluate_documents(
                    workloads, client, config, LogicalClock(), traces.append
                )

    observations = asyncio.run(run())
    assert effects.reaped
    assert endpoint.scored_requests == 8
    assert len(traces) == 10
    assert sum(trace["phase"] == "warmup" for trace in traces) == 2
    assert {trace["document_index"] for trace in traces} == {0, 1}
    assert len(observations) == 8
    for index, workload in enumerate(workloads):
        observed = observations[index * 4 : (index + 1) * 4]
        assert {row.answer for row in observed} == set(workload.facts.values())


class FatalGenerationMonitor:
    """Signal a worker failure once all scored streams are unfinished."""

    def __init__(self) -> None:
        self.failed = asyncio.Event()
        self.active = 0
        self.maximum_active = 0
        self.cancelled_streams = 0
        self.details = {
            "type": "RuntimeError",
            "message": "[METAL] Insufficient Memory: kIOGPUCommandBufferCallbackErrorOutOfMemory",
            "evidence": "generation worker traceback",
        }

    async def wait_for_failure(self) -> dict[str, Any]:
        await self.failed.wait()
        return self.details

    def check(self) -> dict[str, Any] | None:
        return self.details if self.failed.is_set() else None


class UnfinishedGenerationResponse(httpx.AsyncByteStream):
    """A partial stream that remains open until its consumer is cancelled."""

    def __init__(self, answer: str, monitor: FatalGenerationMonitor) -> None:
        self.answer = answer
        self.monitor = monitor
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield event(
            {
                "choices": [
                    {"index": 0, "delta": {"content": self.answer[:4]}, "finish_reason": None}
                ]
            }
        )
        self.monitor.active += 1
        self.monitor.maximum_active = max(self.monitor.maximum_active, self.monitor.active)
        if self.monitor.active == 4:
            self.monitor.failed.set()
        try:
            await self.release.wait()
        finally:
            self.monitor.active -= 1
            self.monitor.cancelled_streams += 1


class FatalGenerationEndpoint(InMemoryServingEndpoint):
    """Complete warmup, then retain all four open streams until worker failure."""

    def __init__(self, workload: Any, monitor: FatalGenerationMonitor) -> None:
        super().__init__(workload)
        self.monitor = monitor

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 120
        payload = json.loads(request.content)
        key = json.dumps(payload["messages"], sort_keys=True)
        if key == self.warmup_messages:
            return await super().handle_async_request(request)
        self.scored_requests += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=UnfinishedGenerationResponse(self.answers[key], self.monitor),
        )


def test_fatal_generation_error_cancels_all_unfinished_streams_without_timeouts() -> None:
    from evaluator import ServerGenerationError

    config = WorkloadConfig()
    workload = generate_workload(config, CharacterTokenizer(), 938)
    traces: list[dict[str, Any]] = []

    async def run() -> tuple[FatalGenerationMonitor, FatalGenerationEndpoint]:
        monitor = FatalGenerationMonitor()
        endpoint = FatalGenerationEndpoint(workload, monitor)
        async with httpx.AsyncClient(
            transport=endpoint, base_url="http://fake.invalid", timeout=httpx.Timeout(120)
        ) as client:
            with pytest.raises(ServerGenerationError) as caught:
                await evaluate_workload(
                    workload, client, config, LogicalClock(), traces.append, monitor=monitor
                )
            assert caught.value.details == monitor.details
        return monitor, endpoint

    monitor, endpoint = asyncio.run(run())
    assert endpoint.scored_requests == 4
    assert monitor.maximum_active == 4
    assert monitor.active == 0
    assert monitor.cancelled_streams == 4
    scored = [trace for trace in traces if trace["phase"] == "scored"]
    assert len(scored) == 4
    assert all(trace["status"] == "failed" for trace in scored)
    assert all(trace["stream"]["done"] is False for trace in scored)
    assert all(trace["stream"]["answer"] for trace in scored)


class WorkerFailureServerEffects(InMemoryServerEffects):
    """Retain worker traceback while the HTTP parent stays alive until cleanup."""

    def launch(
        self, command: list[str], root: Path, environment: dict[str, str], log: Any
    ) -> InMemoryServerProcess:
        process = super().launch(command, root, environment, log)
        log.write(
            "Exception in thread Thread-1 (_generate):\nRuntimeError: [METAL] Insufficient Memory: kIOGPUCommandBufferCallbackErrorOutOfMemory\n"
        )
        log.flush()
        return process


def test_cleanup_sigterm_does_not_replace_generation_worker_failure(tmp_path: Path) -> None:
    from evaluator import build_failure_report

    effects = WorkerFailureServerEffects()
    with pytest.raises(httpx.ReadTimeout) as caught:
        with owned_server(tmp_path, tmp_path / "model", tmp_path, effects=effects):
            raise httpx.ReadTimeout("")
    report = build_failure_report(caught.value, tmp_path)
    server = report["server"]["metadata"]
    assert server["exit_code_before_cleanup"] is None
    assert server["cleanup_requested"] is True
    assert server["exit_code"] == -15
    assert server["exit_cause"] == "cleanup"
    assert report["exception"]["type"] == "ReadTimeout"
    assert "kIOGPUCommandBufferCallbackErrorOutOfMemory" in report["server"]["stderr_tail"]


@pytest.mark.parametrize("worker", ["_generate", "another_thread"])
def test_generation_log_monitor_matches_uncaught_generation_thread_only(
    tmp_path: Path, worker: str
) -> None:
    from evaluator import LogGenerationMonitor

    path = tmp_path / "server.log"
    oom = "RuntimeError: [METAL] Insufficient Memory: kIOGPUCommandBufferCallbackErrorOutOfMemory"
    path.write_text(
        f"Exception in thread Thread-1 ({worker}):\nTraceback (most recent call last):\n{oom}\n"
    )
    evidence = LogGenerationMonitor(path).check()
    if worker == "_generate":
        assert evidence is not None
        assert oom in evidence["stderr_tail"]
    else:
        assert evidence is None


def test_generation_log_monitor_ignores_normal_initialization_and_prefill(tmp_path: Path) -> None:
    from evaluator import LogGenerationMonitor

    path = tmp_path / "server.log"
    path.write_text(
        "UserWarning: tokenizer configuration contains an unrecognized optional key.\n"
        "FutureWarning: Transformers default tokenization behavior may change.\n"
        "INFO - Prompt Cache: 1 sequences, 0.47 GB\n"
        "INFO - Prompt processing progress: 2048/4096\n"
        "INFO - Prompt processing progress: 4096/4096\n"
    )
    assert LogGenerationMonitor(path).check() is None
