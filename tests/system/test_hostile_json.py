"""Every decoder of untrusted JSON answers the one hostile corpus with its value or its own rejection.

The corpus is ``vs_sim.api.testing.hostile_json_frames``: nesting deeper than
any parser's recursion limit, a line past the transport bound, unknown keys,
frames that are not objects, invalid UTF-8, and every prefix of a valid frame
(a peer hanging up mid-frame). Each row below is one decoder and the only
exceptions it may raise. Python's ``json`` raised ``RecursionError`` on deep
nesting and ``UnicodeDecodeError`` on bad bytes, and each escaped a handler
that caught only decoding errors, so a new decoder of peer, agent, evaluator
or on-disk JSON joins by adding one row here.

The transports carry the server decoders over ``SimNetwork`` in
``tests/server/test_unix_transport_faults.py``; this suite pins each decoder's
own promise.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import importlib.util
import io
import json
from contextlib import redirect_stdout, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from resources.profilers.rocprof import analyze_rocprof, att, counters
from tests.server.support import REQUEST_TIME

from server.api.protocol import Response, SubscribeRequest, decode_request
from server.control_envelope import ResponseEnvelopeReader
from server.host_api import (
    HelperError,
    LiveInstanceRecord,
    UnreadableRecord,
    parse_helper_message,
    parse_helper_request,
    parse_record,
)
from server.host_helper import ChannelState, Received, Send, step
from server.transport.discovery import WebInstanceRecord
from vibesys.run import CoreAgentEventSink
from vs_decode.api import Unreadable, decode_json, read_json
from vs_evaluator_protocol.api import ProtocolError, parse_records
from vs_runtime.api import CommandResult
from vs_runtime.api.infrastructure import (
    LocalValidationRecipeError,
    LocalValidator,
    ModelRequestError,
    ProtocolBenchmarkContract,
    ScalarBenchmarkContract,
    TrustedBenchmarkContract,
    create_model_request_reconciler,
    decode_trusted_benchmark_run,
)
from vs_runtime.api.testing import (
    FakeCommands,
    FakeModelVolumeProvisioner,
    FakeWorkspace,
    MemoryValidationPasses,
)
from vs_sim.api import ProcessSpec
from vs_sim.api.testing import (
    OVERSIZED_BYTES,
    FakeProcesses,
    ManualClock,
    ProcessScript,
    hostile_json_frames,
)
from vs_slurm.api import SlurmConfig, SlurmConnectorTransport, slurm_transport_broker_policy

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_VALID = (
    SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0).model_dump_json().encode(),
    json.dumps(
        {"type": "hello", "request_id": "h", "client_versions": [1, 1], "app_version": "v"}
    ).encode(),
    json.dumps({"version": 1, "id": "0123456789ab", "pid": 1}).encode(),
)
_CASES = hostile_json_frames(_VALID)
_MODEL_MANIFEST = ".vibesys/models.json"
"""Where an agent asks for model volumes; its own test file states the same path."""


@dataclass(frozen=True)
class _Decoder:
    """One decoder of untrusted bytes, and the exceptions it may answer with."""

    decode: Callable[[bytes, Path], object]
    """Decode one frame; the directory is the test's own, for decoders that read files."""
    rejections: tuple[type[Exception], ...] = ()
    """Empty for a decoder that answers every frame with a value."""


def _decode_request(frame: bytes, _: Path) -> None:
    result = decode_request(frame, clock=ManualClock())
    assert isinstance(result, Response) or result.type


def _parse_record(frame: bytes, _: Path) -> None:
    assert isinstance(parse_record("0123456789ab", frame), LiveInstanceRecord | UnreadableRecord)


def _helper_channel(frame: bytes, _: Path) -> None:
    """A helper channel's first line: a request, or a refusal it sends back."""
    outputs = step(ChannelState(), Received(frame, 0.0), "v")[1]
    assert frame in _VALID or all(
        isinstance(output, Send) and isinstance(output.message, HelperError) for output in outputs
    )


def _web_instance_record(frame: bytes, directory: Path) -> None:
    path = directory / "web-gateway.json"
    path.write_bytes(frame)
    WebInstanceRecord.read(path)


def _model_manifest(frame: bytes, workspace: Path) -> None:
    manifest = workspace / _MODEL_MANIFEST
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_bytes(frame)
    create_model_request_reconciler(
        provisioner=FakeModelVolumeProvisioner(), environment={}
    ).reconcile(workspace)


_BENCHMARK_CONTRACTS: tuple[TrustedBenchmarkContract, ...] = (
    ProtocolBenchmarkContract(),
    ScalarBenchmarkContract(output_argument="--out", metric="tok_s"),
)


def _benchmark_result(contract: TrustedBenchmarkContract) -> _Decoder:
    def decode(frame: bytes, _: Path) -> None:
        framed = (
            "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n"
            + frame.decode("utf-8", "replace")
            + "\n__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__\n"
        )
        decoded = decode_trusted_benchmark_run(framed, contract, frozenset(), exited_cleanly=True)
        assert decoded.passed is False

    return _Decoder(decode)


@functools.cache
def _otel_server() -> ModuleType:
    """The otel profiler's MCP server, loaded from its file as the profiler bundle runs it."""
    path = Path(__file__).parents[2] / "resources" / "profilers" / "otel" / "server.py"
    spec = importlib.util.spec_from_file_location("_hostile_json_otel_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _otel_artifacts(frame: bytes, directory: Path) -> None:
    """A candidate's workspace JSON that the otel profiler scans for reports and graphs."""
    (directory / "artifact.json").write_bytes(frame)
    server = _otel_server()
    assert server.find_reports(str(directory)) == []
    assert server.find_trace_graphs(str(directory)) == []


_capture_runtime = analyze_rocprof.capture_runtime
"""The profilers' stdlib-only capture runtime, as the analyzers load it."""
_KERNEL_TRACE = "Kernel_Name,Start_Timestamp,End_Timestamp\nkernel,0,100\n"


def _capture_manifest(frame: bytes, capture: Path) -> None:
    (capture / "manifest.json").write_bytes(frame)
    _capture_runtime.load_manifest(capture)


def _capture_listing(frame: bytes, root: Path) -> None:
    """A capture whose manifest is hostile is listed, with no kind or status from it."""
    capture = root / "capture"
    capture.mkdir(exist_ok=True)
    (capture / "manifest.json").write_bytes(frame)
    [listed] = _capture_runtime.list_captures(root=root)
    assert isinstance(listed.kind, str | None)
    assert isinstance(listed.status, str | None)


def _rocprof_capture_manifest(frame: bytes, report: Path) -> None:
    """A kernel report beside a hostile capture manifest still analyzes, unwindowed."""
    (report / "out_kernel_trace.csv").write_text(_KERNEL_TRACE)
    (report / "manifest.json").write_bytes(frame)
    with redirect_stdout(io.StringIO()):
        analyze_rocprof.cmd_kernels(argparse.Namespace(report=str(report), top=15, window="load"))


def _rocprof_json_report(frame: bytes, report: Path) -> None:
    (report / "results.json").write_bytes(frame)
    with redirect_stdout(io.StringIO()):
        analyze_rocprof.cmd_kernels(argparse.Namespace(report=str(report), top=15, window="all"))


def _rocprof_counters(frame: bytes, report: Path) -> None:
    (report / "out_counter_collection.json").write_bytes(frame)
    with redirect_stdout(io.StringIO()):
        counters.cmd_report(argparse.Namespace(dirs=[str(report)], kernel=None, arch=None, top=15))


def _rocprof_thread_trace(frame: bytes, dispatch: Path) -> None:
    (dispatch / att.CODE_JSON_NAME).write_bytes(frame)
    att.load_instructions(dispatch)


_RECIPES = {
    "version": 1,
    "recipes": [
        {
            "name": "focused",
            "command": "pytest -q",
            "input_paths": ["queue.py"],
            "timeout_seconds": 30,
            "purpose": "exercise the candidate",
        }
    ],
}


def _local_validation(workspace: Path) -> tuple[object, ...]:
    commands = FakeCommands()
    commands.script(CommandResult(output="passed\n", exit_code=0))
    return asyncio.run(
        LocalValidator(commands, MemoryValidationPasses()).run(
            FakeWorkspace(path=workspace),
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
        )
    )


def _validation_report(frame: bytes, workspace: Path) -> None:
    """A file the candidate left beside the validation report is not reused, and runs anew."""
    recipes = workspace / "validation" / "recipes.json"
    if not recipes.exists():
        recipes.parent.mkdir()
        (workspace / "queue.py").write_text("VALUE = 1\n")
        recipes.write_text(json.dumps(_RECIPES))
    (workspace / "validation" / "planted.json").write_bytes(frame)
    [result] = _local_validation(workspace)
    assert getattr(result, "passed", None) is True
    assert getattr(result, "reused", None) is False


def _validation_recipes(frame: bytes, workspace: Path) -> None:
    (workspace / "validation").mkdir(exist_ok=True)
    (workspace / "validation" / "recipes.json").write_bytes(frame)
    _local_validation(workspace)


def _tool_result(frame: bytes, _: Path) -> None:
    payloads: list[object] = []
    CoreAgentEventSink(payloads.append, ManualClock()).tool_result(
        "shell", frame.decode("utf-8", "replace")
    )
    assert len(payloads) == 1


class _Hello(BaseModel):
    """A strict document shaped like one of the valid frames, for the decode function."""

    model_config = ConfigDict(extra="forbid")

    type: str
    request_id: str
    client_versions: list[int]
    app_version: str


def _decode_json_file(frame: bytes, workspace: Path) -> None:
    """The framework's decode function: a file's value or a typed ``Unreadable``, never a raise."""
    path = workspace / "document.json"
    path.write_bytes(frame)
    decoded = read_json(path, _Hello, max_bytes=OVERSIZED_BYTES - 1)
    assert isinstance(decoded, _Hello) or (decoded.source == str(path) and decoded.problems)
    assert isinstance(
        decode_json(frame, _Hello, source="frame", max_bytes=OVERSIZED_BYTES - 1),
        _Hello | Unreadable,
    )


def _connector_broker_request(frame: bytes, workspace: Path) -> None:
    """The host broker's connector edge either authorizes a typed call or safely refuses it."""
    config = SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=SlurmConnectorTransport(kind="connector", command=("connector",)),
    )
    policy = slurm_transport_broker_policy(config, local_roots=(workspace,))
    policy.run(
        ProcessSpec(("connector",), input=frame),
        processes=FakeProcesses(
            lambda _spec: ProcessScript(
                stdout=b'{"version":1,"returncode":0,"stdout":"","stderr":""}'
            ),
            pass_time=ManualClock().advance,
        ),
        timeout=1,
    )


_DECODERS = {
    "decode-json": _Decoder(_decode_json_file),
    "slurm-connector-broker": _Decoder(_connector_broker_request, (PermissionError,)),
    "protocol-request": _Decoder(_decode_request),
    "instance-record": _Decoder(_parse_record),
    "helper-channel": _Decoder(_helper_channel),
    "helper-request": _Decoder(lambda frame, _: parse_helper_request(frame)),
    "helper-message": _Decoder(lambda frame, _: parse_helper_message(frame)),
    "response-envelope": _Decoder(
        lambda frame, _: ResponseEnvelopeReader.model_validate_json(frame), (ValidationError,)
    ),
    "web-instance-record": _Decoder(_web_instance_record),
    "evaluator-records": _Decoder(
        lambda frame, _: parse_records(frame.decode("utf-8", "replace")), (ProtocolError,)
    ),
    **{
        f"{type(contract).__name__}-result": _benchmark_result(contract)
        for contract in _BENCHMARK_CONTRACTS
    },
    "model-manifest": _Decoder(_model_manifest, (ModelRequestError,)),
    "agent-tool-result": _Decoder(_tool_result),
    "local-validation-report": _Decoder(_validation_report),
    "local-validation-recipes": _Decoder(_validation_recipes, (LocalValidationRecipeError,)),
    "otel-artifact-discovery": _Decoder(_otel_artifacts),
    "capture-manifest": _Decoder(_capture_manifest, (ValueError,)),
    "capture-listing": _Decoder(_capture_listing),
    "rocprof-capture-manifest": _Decoder(_rocprof_capture_manifest),
    "rocprof-json-report": _Decoder(_rocprof_json_report, (_capture_runtime.CaptureFailedError,)),
    "rocprof-counters": _Decoder(_rocprof_counters, (_capture_runtime.CaptureFailedError,)),
    "rocprof-thread-trace": _Decoder(_rocprof_thread_trace, (att.AttOutputNotFoundError,)),
}


def test_every_benchmark_contract_has_a_decoder_row() -> None:
    """A new `TrustedBenchmarkContract` member fails here until `_BENCHMARK_CONTRACTS` lists it."""
    members = set(get_args(TrustedBenchmarkContract.__value__))
    assert {type(contract) for contract in _BENCHMARK_CONTRACTS} == members


@pytest.mark.parametrize("decoder", list(_DECODERS.values()), ids=list(_DECODERS))
def test_a_decoder_answers_every_hostile_frame_with_its_value_or_its_own_rejection(
    decoder: _Decoder, tmp_path: Path
) -> None:
    for name, frame in _CASES:
        with suppress(*decoder.rejections):
            try:
                decoder.decode(frame, tmp_path)
            except Exception as error:
                if isinstance(error, decoder.rejections):
                    raise
                error.add_note(f"hostile case: {name}")
                raise
