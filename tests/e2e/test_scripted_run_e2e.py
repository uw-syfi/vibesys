"""An agent-less run: the real backend, scripted agents, no model.

The run is built from the command line as an operator builds it (``--agent-backend scripted``) and
goes through the production launcher and host composition: real Git worktrees, the runtime loop and
the trusted evaluation scripts, on the executing Fake Slurm cluster. Nothing is substituted but the
cluster, the state stores and the agents, which are the scripted ones. The cluster answers in
process (a connector process per request cost a third of the run), and the state stores are the
Fake that commits and fences as the product's do: a run commits over a thousand times, each commit
of the on-disk store decodes the whole document, and that was half of the run. What the run proves
does not depend on durability, which the state-store contract and durability tests cover. The
project is the harness's small CPU one (``queue.py`` holds the throughput), so the scenario runs in
seconds; the scripted agents get a workload of their own for it, because their built-in one edits
the Rust queue example.
The edits of that example are built and measured natively by ``test_scripted_queue_recipes_e2e``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.e2e.dynamic_loop_harness import _AGENT_CONFIG, LoopInput, resume_request, run_request
from tests.e2e.scripted_value_run import SEED, NoAgents, scripted_client
from tests.support.real_git import REAL_GIT

import vs_proto.api as p
from vibesys.api import PauseRequest, RunHandle, RunStatus
from vibesys.events import CoreEventType, WorkstreamFinishedData
from vibesys.orchestration.dynamic.scripted.api import (
    IMPLEMENTER_KIND,
    JUDGE_KIND,
    PLANNER_KIND,
)
from vs_project.api import NullGitTrackerEvents, Project, open_git_repository
from vs_runlog.api import RunLogReader

if TYPE_CHECKING:
    from pathlib import Path


ROUNDS = 3


def test_scripted_agents_plan_edit_and_judge_through_the_real_backend(tmp_path: Path) -> None:
    loop_input = LoopInput.create(
        tmp_path, agent_config=f"{_AGENT_CONFIG}[agent]\nscripted_seed = {SEED}\n"
    )
    request = loop_input.request(agent_backend="scripted", max_rounds=ROUNDS, max_in_flight=2)

    run = run_request(
        request,
        NoAgents(),
        client_factory=scripted_client,
        slurm_processes=loop_input.connector,
        verify=False,
    )

    run.raise_error()
    assert run.status is RunStatus.COMPLETED
    finished = [e for e in run.events if e.type is CoreEventType.INVOCATION_FINISHED]
    kinds = {event.agent_kind for event in finished}
    assert {PLANNER_KIND, IMPLEMENTER_KIND, JUDGE_KIND} <= kinds
    types = [event.type for event in run.events]
    assert types.count(CoreEventType.ROUND_FINISHED) >= 1
    # Some workstreams end measured, some end for a reason: a lost turn, a judge's rejection.
    ended = [
        event.data.last_error
        for event in run.events
        if isinstance(event.data, WorkstreamFinishedData)
    ]
    assert None in ended
    assert any(error is not None for error in ended)
    # The agents narrate their turns, so a frontend has something to show for each.
    assert CoreEventType.AGENT_OUTPUT_CHUNK in types


def test_scripted_run_relaunches_once_from_saved_state(tmp_path: Path) -> None:
    loop_input = LoopInput.create(
        tmp_path,
        on_disk_state=True,
        agent_config=f"{_AGENT_CONFIG}[agent]\nscripted_seed = {SEED}\n",
    )
    request = loop_input.request(agent_backend="scripted", max_rounds=1, max_in_flight=1)
    stopped: list[asyncio.Task[None]] = []

    async def stop_after_start(handle: RunHandle) -> None:
        async with asyncio.timeout(60):
            async for event in handle.events():
                if event.type is CoreEventType.AGENT_EXECUTION_STARTED:
                    handle.stop()
                    return
        message = "the scripted run ended before starting an agent"
        raise AssertionError(message)

    first = run_request(
        request,
        NoAgents(),
        client_factory=scripted_client,
        slurm_processes=loop_input.connector,
        verify=False,
        on_handle=lambda handle: stopped.append(asyncio.create_task(stop_after_start(handle))),
    )
    first.raise_error()
    assert first.status is RunStatus.STOPPED
    assert len(stopped) == 1
    stopped[0].result()
    second = run_request(
        resume_request(request, first.run_id),
        NoAgents(),
        client_factory=scripted_client,
        slurm_processes=loop_input.connector,
        verify=False,
    )
    second.raise_error()
    assert second.run_id == first.run_id
    with Project.run_log_path_for(loop_input.root, first.run_id).open("rb") as stream:
        records = list(RunLogReader(stream))
    headers = [r.body.value.epoch for r in records if r.body and r.body.field == "epoch_started"]
    assert set(headers) == {1, 2}
    ends = [
        (r.epoch, r.body.value.state.value.outcome)
        for r in records
        if r.body
        and r.body.field == "lifecycle"
        and r.body.value.state is not None
        and r.body.value.state.field == "ended"
    ]
    assert [epoch for epoch, _ in ends] == [1, 2]
    assert ends[0][1] in {p.Outcome.STOPPED, p.Outcome.INTERRUPTED}
    assert [r.clock for r in records] == sorted(r.clock for r in records)


def test_a_scripted_run_pauses_then_resumes_without_overwriting_the_project(tmp_path: Path) -> None:
    loop_input = LoopInput.create(
        tmp_path,
        on_disk_state=True,
        agent_config=f"{_AGENT_CONFIG}[agent]\nscripted_seed = {SEED}\n",
    )
    request = loop_input.request(agent_backend="scripted", max_rounds=ROUNDS, max_in_flight=2)

    original = (loop_input.root / "queue.py").read_bytes()

    def pause_on_start(handle: RunHandle) -> None:
        # This fresh run has no predecessor. Closing controls explicitly target its first epoch.
        handle.session.submit(PauseRequest(control_id="pause-before-start", expected_epoch=1))

    parked = run_request(
        request,
        NoAgents(),
        client_factory=scripted_client,
        slurm_processes=loop_input.connector,
        on_handle=pause_on_start,
        verify=False,
    )
    parked.raise_error()
    assert parked.status is RunStatus.PARKED
    types = [event.type for event in parked.events]
    assert CoreEventType.PARK_REQUESTED in types
    assert CoreEventType.PARKED in types
    assert CoreEventType.RUN_FAILED not in types
    assert CoreEventType.STOPPED not in types
    assert CoreEventType.RUN_ENDING not in types
    with Project.run_log_path_for(loop_input.root, parked.run_id).open("rb") as stream:
        records = list(RunLogReader(stream))
    lifecycle = [
        record.body.value.state.field
        for record in records
        if record.body and record.body.field == "lifecycle" and record.body.value.state is not None
    ]
    assert lifecycle[-2:] == ["draining", "parked"]
    assert "ended" not in lifecycle
    repository = open_git_repository(loop_input.root, git=REAL_GIT, faults=NullGitTrackerEvents())
    home = "operator-work"
    repository.switch_branch(home, create=True)
    edited = original + b"\n# operator notes\n"
    (loop_input.root / "queue.py").write_bytes(edited)
    repository.stage_all(["queue.py"])
    (loop_input.root / "notes.txt").write_text("keep my notes", encoding="utf-8")
    before = repository.uncommitted_paths(["queue.py", "notes.txt"])
    resumed = run_request(
        resume_request(request, parked.run_id),
        NoAgents(),
        client_factory=scripted_client,
        slurm_processes=loop_input.connector,
        verify=False,
        relaunch_saved=True,
    )
    resumed.raise_error()
    # P1 resumes the search but guards publication until project-edit protection lands.
    assert resumed.status is RunStatus.FAILED
    assert repository.current_branch() == home
    assert (loop_input.root / "queue.py").read_bytes() == edited
    assert (loop_input.root / "notes.txt").read_text(encoding="utf-8") == "keep my notes"
    assert repository.has_staged_changes(["queue.py"])
    assert repository.uncommitted_paths(["queue.py", "notes.txt"]) == before
    assert resumed.run_id == parked.run_id
    with Project.run_log_path_for(loop_input.root, resumed.run_id).open("rb") as stream:
        assert max(record.epoch for record in RunLogReader(stream)) == 2
