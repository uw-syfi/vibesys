"""The run's log owners are a strict wire contract, and the run's store keeps them typed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.api import (
    AgentInvocation,
    GateStage,
    RunNarration,
    SlurmJob,
    StartupStep,
    ToolBuild,
)
from vibesys.events import GateKind
from vibesys.log_owners import LOG_OWNER
from vibesys.run.logs import RUN_LOG_OWNER, open_run_log_store
from vs_logs.read import Tail, open_log_directory
from vs_project.api import Project
from vs_runtime.api.infrastructure import EnvironmentStartupStage
from vs_sim.api import OutputStream
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
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
    st.builds(AgentInvocation, invocation_id=_IDS),
    st.builds(ToolBuild, name=_IDS),
    st.builds(SlurmJob, execution_id=_IDS, job_id=_IDS),
    st.just(RunNarration()),
)


@given(owner=_OWNERS)
def test_every_owner_round_trips_through_json(
    owner: StartupStep | GateStage | AgentInvocation | ToolBuild | SlurmJob | RunNarration,
) -> None:
    assert LOG_OWNER.validate_json(LOG_OWNER.dump_json(owner)) == owner


@given(owner=_OWNERS, key=st.text(min_size=1, max_size=8))
def test_an_unknown_key_on_any_owner_is_rejected(
    owner: StartupStep | GateStage | AgentInvocation | ToolBuild | SlurmJob | RunNarration,
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
    store = open_run_log_store(tmp_path, "run-1", threads=SimThreads())
    with store.open(StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE), "docker build") as s:
        s.write(OutputStream.STDERR, b"step 1/3\n")

    reader = open_log_directory(
        Project.log_directory_for(tmp_path, "run-1") / "streams", RUN_LOG_OWNER
    )
    (info,) = reader.list_streams()
    assert info.owner == StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
    assert reader.read(info.stream_id, Tail(100), 100).data == b"step 1/3\n"
