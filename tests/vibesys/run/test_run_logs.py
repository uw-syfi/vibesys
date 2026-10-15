"""The run's log owners are a strict wire contract, and the run's store keeps them typed."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.api import GateStage, RunNarration, StartupStep, ToolBuild
from vibesys.events import GateKind
from vibesys.log_owners import LOG_OWNER
from vibesys.run.logs import open_run_log_store, open_run_logs
from vs_logs.api import NarrationHandler, NarrationLevel, NarrationLine, StreamOutcome
from vs_logs.api.testing import no_warning
from vs_logs.read import Tail, open_log_directory
from vs_project.api import Project
from vs_runtime.api.infrastructure import EnvironmentStartupStage
from vs_sim.api import OutputStream
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_IDS = st.text(min_size=1, max_size=12)
_OWNERS = st.one_of(
    st.builds(StartupStep, stage=st.sampled_from(list(EnvironmentStartupStage))),
    st.builds(
        GateStage,
        execution_id=_IDS,
        gate=st.sampled_from(list(GateKind)),
        attempt=st.integers(min_value=0, max_value=50),
    ),
    st.builds(ToolBuild, name=_IDS),
    st.just(RunNarration()),
)


@given(owner=_OWNERS)
def test_every_owner_round_trips_through_json(
    owner: StartupStep | GateStage | ToolBuild | RunNarration,
) -> None:
    assert LOG_OWNER.validate_json(LOG_OWNER.dump_json(owner)) == owner


@given(owner=_OWNERS, key=st.text(min_size=1, max_size=8))
def test_an_unknown_key_on_any_owner_is_rejected(
    owner: StartupStep | GateStage | ToolBuild | RunNarration,
    key: str,
) -> None:
    data = LOG_OWNER.dump_python(owner, mode="json")
    if key in data:
        return
    with pytest.raises(ValidationError):
        LOG_OWNER.validate_python({**data, key: 1})


def test_an_unknown_owner_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LOG_OWNER.validate_python({"kind": "server_process"})


def test_the_run_store_lives_in_the_runs_log_directory_and_reads_back_typed_owners(
    tmp_path: Path,
) -> None:
    store = open_run_log_store(tmp_path, "run-1", threads=SimThreads(), warn=no_warning)
    with store.open(StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE), "docker build") as s:
        s.write(OutputStream.STDERR, b"step 1/3\n")

    reader = open_log_directory(Project.log_streams_directory_for(tmp_path, "run-1"), LOG_OWNER)
    (info,) = reader.list_streams()
    assert info.owner == StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
    assert reader.read(info.stream_id, Tail(100), 100).data == b"step 1/3\n"


@contextmanager
def _narration_logger(name: str, handler: NarrationHandler) -> Iterator[logging.Logger]:
    """A logger of the test's own that does not propagate, so the root logger is left alone."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        logger.removeHandler(handler)


def test_the_run_opens_one_narration_stream_and_flushes_what_was_logged_before(
    tmp_path: Path,
) -> None:
    warnings: list[str] = []
    handler = NarrationHandler(warn=warnings.append)
    with _narration_logger("vibesys.run.test", handler) as logger:
        logger.info("before the store opened")
        with open_run_logs(
            tmp_path, "run-1", threads=SimThreads(), warn=warnings.append, narration=handler
        ) as scope:
            logger.warning("while the run runs")
            build = StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
            with scope.open(build, "build") as stream:
                stream.write(OutputStream.STDOUT, b"step\n")
        logger.error("after the run closed its logs")

    reader = open_log_directory(Project.log_streams_directory_for(tmp_path, "run-1"), LOG_OWNER)
    narration, startup = reader.list_streams()
    assert (narration.owner, narration.outcome) == (RunNarration(), StreamOutcome.ENDED)
    assert startup.owner == StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
    data = reader.read(narration.stream_id, Tail(1 << 16), 1 << 16).data
    lines = [NarrationLine.model_validate_json(line) for line in data.splitlines()]
    assert [(line.level, line.logger, line.message) for line in lines] == [
        (NarrationLevel.INFO, "vibesys.run.test", "before the store opened"),
        (NarrationLevel.WARNING, "vibesys.run.test", "while the run runs"),
    ]
    assert warnings == []


def test_a_run_whose_store_cannot_open_goes_on_and_says_so(tmp_path: Path) -> None:
    streams = Project.log_streams_directory_for(tmp_path, "run-1")
    streams.parent.mkdir(parents=True)
    streams.write_text("a file where the store's directory goes")
    warnings: list[str] = []
    handler = NarrationHandler(warn=warnings.append)
    with (
        _narration_logger("vibesys.run.unavailable", handler) as logger,
        open_run_logs(
            tmp_path, "run-1", threads=SimThreads(), warn=warnings.append, narration=handler
        ) as scope,
    ):
        logger.info("still logging")
        with scope.open(RunNarration(), "anything") as stream:
            stream.write(OutputStream.STDOUT, b"kept nowhere visible\n")
    assert len(warnings) == 1
    assert "unavailable" in warnings[0]
    assert not handler.waiting


def test_a_run_whose_store_index_is_corrupt_goes_on_and_says_so(tmp_path: Path) -> None:
    """A store that cannot be read is unavailable to the run, never a failure of it."""
    streams = Project.log_streams_directory_for(tmp_path, "run-1")
    streams.mkdir(parents=True)
    (streams / "index.jsonl").write_bytes(b'{"kind":"opened","stream_id":"000001"\n')
    warnings: list[str] = []
    with (
        open_run_logs(
            tmp_path, "run-1", threads=SimThreads(), warn=warnings.append, narration=None
        ) as scope,
        scope.open(RunNarration(), "anything") as stream,
    ):
        stream.write(OutputStream.STDOUT, b"kept nowhere visible\n")
    assert len(warnings) == 1
    assert "unavailable" in warnings[0]


def test_a_resumed_run_keeps_its_logs_after_a_torn_index_line(tmp_path: Path) -> None:
    """A full disk that tore the index's last line costs that record, not the store."""
    build = StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
    warnings: list[str] = []

    def run(output: bytes) -> None:
        with (
            open_run_logs(
                tmp_path, "run-1", threads=SimThreads(), warn=warnings.append, narration=None
            ) as scope,
            scope.open(build, "build") as stream,
        ):
            stream.write(OutputStream.STDOUT, output)

    run(b"first\n")
    index = Project.log_streams_directory_for(tmp_path, "run-1") / "index.jsonl"
    with index.open("ab") as torn:
        torn.write(b'{"kind":"opened","stream_id":"0000')
    run(b"resumed\n")

    reader = open_log_directory(Project.log_streams_directory_for(tmp_path, "run-1"), LOG_OWNER)
    first, resumed = reader.list_streams()
    assert [first.outcome, resumed.outcome] == [StreamOutcome.ENDED, StreamOutcome.ENDED]
    assert reader.read(resumed.stream_id, Tail(100), 100).data == b"resumed\n"
    assert warnings == []
