"""A gate's stage streams name the gate's run-trace span, so ``logs.list`` finds them by it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from server.host_api import LogsListReply, LogsListRequest
from server.host_helper_logs import HelperLogs, RunLocation, list_logs
from server.host_helper_os import StoreLogReader, run_streams_directory
from vibesys.run.logs import open_run_log_store, stage_output_opener
from vs_runtime.api import TryHandles
from vs_runtime.api.core import MeasurementDecisions, StageRun
from vs_sim.api import OutputStream
from vs_sim.api.testing import SimThreads, examples

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_RUN = "20261015-120000-11111111-run"
_STAGES = ("accuracy", "benchmark")


def _logs(root: Path, threads: SimThreads) -> HelperLogs:
    reader = StoreLogReader(lambda _instance: RunLocation(str(root), _RUN), run_streams_directory)
    return HelperLogs(reader, threads)


def _list(root: Path, threads: SimThreads, span: str) -> tuple[tuple[str, str], ...]:
    reply = list_logs(
        LogsListRequest(request_id="l", deadline_ms=1, instance_id="0123456789ab", span_id=span),
        _logs(root, threads),
    )
    assert isinstance(reply, LogsListReply)
    return tuple((str(stream.owner["execution_id"]), stream.command) for stream in reply.streams)


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(
    measurements=st.lists(
        st.tuples(st.sampled_from(["a", "b", "c"]), st.sampled_from(_STAGES), st.integers(1, 2)),
        min_size=1,
        max_size=6,
    )
)
def test_logs_list_by_a_gate_span_returns_exactly_that_gates_stage_streams(
    tmp_path_factory: pytest.TempPathFactory, measurements: list[tuple[str, str, int]]
) -> None:
    root = tmp_path_factory.mktemp("project")
    threads = SimThreads()
    store = open_run_log_store(root, _RUN, threads=threads, warn=lambda _message: None)
    decisions = MeasurementDecisions()
    tries = TryHandles()
    for name in "abc":
        decisions.record(f"vs-{name}", f"decision-{name}")
        tries.record(f"vs-{name}", 2)
    opener = tries.stage_output(decisions.stage_output(stage_output_opener(store)))
    for name, stage, attempt in measurements:
        inner = f"vs-{name}" if attempt == 1 else f"vs-{name}-try-2"
        with opener(StageRun(handle_id=inner, stage_id=stage, purpose="official")) as sink:
            sink.write(OutputStream.STDOUT, b"ran\n")

    for name in "abc":
        expected = tuple((f"vs-{n}", stage) for n, stage, _attempt in measurements if n == name)
        assert _list(root, threads, f"op:decision-{name}") == expected
