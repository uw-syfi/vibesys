"""``vibesys evals``: read and withdraw a project's ledger entries with no run attached (#2757).

Every test runs the command against one Fake scheduler and one Fake ledger.
Entries are made by a run's ledger attachment, the same code a VibeSys run
submits through; the command never sees that run.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from entrypoints.evals import EvalsHost, run
from vs_sandbox.api.slurm import Attached, LedgerWithdrawnError, SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    ClusterObservation,
    EntryPhase,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerError,
    LedgerLabels,
    LedgerListing,
    LedgerPolicy,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    entry_id,
    ledger_operation_id,
)

if TYPE_CHECKING:
    from pathlib import Path

PROJECT = "demo-0123456789ab"
LEASE_S = 300.0
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
)


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = ManualClock(1000.0)
        self.cluster = FakeCluster(clock=self.clock)
        self.ledger = FakeEvaluationLedger(project=PROJECT, clock=self.clock)
        self.run = SlurmLedgerAttachment(
            ledger=self.ledger.reopen(),
            config=CONFIG,
            project=PROJECT,
            holder="run-1",
            policy=LedgerPolicy(lease_seconds=LEASE_S),
            clock=self.clock,
        )
        self.questions: list[str] = []
        self.answer = True

    def submit(self, name: str, *, queue_s: float, run_s: float = math.inf) -> Attached:
        """Attach the run to a request for ``name``, scripted to queue then run on the clock."""
        workspace = self.root / name
        workspace.mkdir(parents=True)
        (workspace / "bench.py").write_text(f"print({name!r})", encoding="utf-8")
        request = SlurmBatchRequest(
            workspace=workspace,
            stages=(SlurmBatchStage(name="benchmark", command=("python", "bench.py")),),
        )
        operation = ledger_operation_id(self.run.key(request), 0)
        self.cluster.script(operation, queue_wait_s=queue_s, run_s=run_s, completing_s=0.0)
        attached = self.run.attach(
            self.cluster, request, labels=LedgerLabels(candidate=name, workstream="ws-1")
        )
        assert isinstance(attached, Attached)
        return attached

    def status(self, attached: Attached) -> SlurmJobStatus:
        observed = self.cluster.inspect(attached.operation_id)
        assert isinstance(observed, ClusterObservation)
        return observed.status

    def confirm(self, question: str) -> bool:
        self.questions.append(question)
        return self.answer

    def evals(
        self, *argv: str, call_seconds: float = CONFIG.transport_timeout_seconds
    ) -> tuple[int, str]:
        host = EvalsHost(
            ledger=lambda project: (
                FakeEvaluationLedger(project=project, clock=ManualClock())
                if project != PROJECT
                else self.ledger.reopen()
            ),
            cluster=self.cluster,
            clock=self.clock,
            call_seconds=call_seconds,
            current_project=lambda: PROJECT,
            confirm=self.confirm,
        )
        return run(list(argv), host)

    def listing(self, *argv: str) -> LedgerListing:
        code, output = self.evals("list", "--json", *argv)
        assert code == 0
        return LedgerListing.model_validate_json(output)


def test_list_shows_each_entry_with_no_run_attached(tmp_path: Path) -> None:
    world = _World(tmp_path)
    queued = world.submit("cand-a", queue_s=math.inf)
    running = world.submit("cand-b", queue_s=0.0)
    world.clock.advance(60.0)

    listing = world.listing()

    rows = {row.key: row for row in listing.rows}
    assert listing.project == PROJECT
    assert rows[queued.key].phase is EntryPhase.QUEUED
    assert rows[queued.key].waiting_seconds == 60.0
    assert rows[running.key].phase is EntryPhase.RUNNING
    assert rows[running.key].waiting_seconds is None
    for attached, row in ((queued, rows[queued.key]), (running, rows[running.key])):
        assert row.entry == entry_id(attached.key)
        assert row.job_id == attached.handle.job.job_id
        assert row.owner == "run-1"
        assert row.lease_holders == ("run-1",)
        assert row.lease_age_seconds == 60.0
        assert row.workstream == "ws-1"
        assert row.result_location is not None
    assert {row.candidate for row in listing.rows} == {"cand-a", "cand-b"}

    assert [row.key for row in world.listing("--state", "queued").rows] == [queued.key]
    code, text = world.evals("list")
    assert code == 0
    assert entry_id(queued.key) in text
    assert entry_id(running.key) in text


def test_list_of_a_project_with_no_entries_says_so(tmp_path: Path) -> None:
    world = _World(tmp_path)
    code, text = world.evals("list", "--project", "other-0123456789ab")
    assert (code, text) == (0, "Project other-0123456789ab has no evaluations in its ledger.\n")


def test_list_names_the_command_that_frees_an_entry_held_by_an_unreadable_lease(
    tmp_path: Path,
) -> None:
    """A lease this build cannot decode is shown with the command that withdraws it now."""
    world = _World(tmp_path)
    attached = world.submit("cand-a", queue_s=math.inf)
    world.ledger.store_raw_lease(
        attached.key, "run-new", '{"format_version": 2}\n', written_at=world.clock.now()
    )

    code, text = world.evals("list")

    assert code == 0
    (line,) = [line for line in text.splitlines() if "unreadable lease from run-new" in line]
    command = f"vibesys evals remove {entry_id(attached.key)}"
    assert command in line
    assert world.evals(*command.split()[2:], "--yes")[0] == 0
    assert world.status(attached) is SlurmJobStatus.CANCELLED


def test_removing_a_queued_entry_means_its_job_never_runs(tmp_path: Path) -> None:
    world = _World(tmp_path)
    queued = world.submit("cand-a", queue_s=100.0, run_s=100.0)

    code, output = world.evals("remove", entry_id(queued.key), "--yes")

    assert code == 0
    assert f"cancelled Slurm job {queued.handle.job.job_id}" in output
    # Past the time it would have queued and run, it still never ran.
    world.clock.advance(1000.0)
    assert world.status(queued) is SlurmJobStatus.CANCELLED
    (row,) = world.listing().rows
    assert row.phase is EntryPhase.WITHDRAWN
    assert world.questions == []


def test_removing_a_running_entry_cancels_its_job(tmp_path: Path) -> None:
    world = _World(tmp_path)
    running = world.submit("cand-a", queue_s=0.0)
    other = world.submit("cand-b", queue_s=0.0)
    world.clock.advance(1.0)
    assert world.status(running) is SlurmJobStatus.RUNNING

    code, output = world.evals("remove", running.key[:8])

    assert code == 0
    assert f"cancelled Slurm job {running.handle.job.job_id}" in output
    assert len(world.questions) == 1
    assert entry_id(running.key) in world.questions[0]
    world.clock.advance(60.0)
    assert world.status(running) is SlurmJobStatus.CANCELLED
    assert world.status(other) is SlurmJobStatus.RUNNING


def test_an_attached_run_observes_a_removal_as_withdrawn(tmp_path: Path) -> None:
    world = _World(tmp_path)
    attached = world.submit("cand-a", queue_s=math.inf)

    assert world.evals("remove", "--all", "--yes")[0] == 0

    # The run reads the ledger again a third of a lease period after its last renewal.
    world.clock.advance(LEASE_S / 3)
    with pytest.raises(LedgerWithdrawnError, match="removed with `vibesys evals remove`"):
        world.run.heartbeat(world.cluster, attached.key, attached.operation_id)


def test_a_declined_confirmation_removes_nothing(tmp_path: Path) -> None:
    world = _World(tmp_path)
    queued = world.submit("cand-a", queue_s=math.inf)
    world.answer = False

    assert world.evals("remove", "--all") == (1, "Nothing removed.\n")
    assert world.status(queued) is SlurmJobStatus.PENDING
    assert world.listing().rows[0].phase is EntryPhase.QUEUED


def test_an_id_that_names_no_entry_removes_nothing(tmp_path: Path) -> None:
    world = _World(tmp_path)
    queued = world.submit("cand-a", queue_s=math.inf)

    code, output = world.evals("remove", entry_id(queued.key), "zzzz", "--yes")

    assert (code, output) == (1, "no entry zzzz\n")
    assert world.status(queued) is SlurmJobStatus.PENDING


def test_remove_needs_ids_or_all_but_not_both(tmp_path: Path) -> None:
    world = _World(tmp_path)
    for argv in (("remove", "--yes"), ("remove", "abc", "--all", "--yes")):
        with pytest.raises(SystemExit):
            world.evals(*argv)


@given(removed=st.sets(st.integers(0, 3), max_size=4))
@settings(max_examples=examples(pr=6, nightly=30), deadline=None)
def test_remove_withdraws_exactly_the_named_entries(
    tmp_path_factory: pytest.TempPathFactory, removed: set[int]
) -> None:
    world = _World(tmp_path_factory.mktemp("world"))
    entries = [world.submit(f"cand-{index}", queue_s=math.inf) for index in range(4)]

    if removed:
        ids = [entry_id(entries[index].key) for index in sorted(removed)]
        code, output = world.evals("remove", *ids, "--yes")
        assert code == 0
        assert output.count("cancelled Slurm job") == len(removed)

    phases = {row.key: row.phase for row in world.listing().rows}
    for index, attached in enumerate(entries):
        expected = EntryPhase.WITHDRAWN if index in removed else EntryPhase.QUEUED
        assert phases[attached.key] is expected


def test_remove_refuses_a_transport_timeout_whose_mark_is_over_the_bound_before_asking(
    tmp_path: Path,
) -> None:
    """A mark at a 200 s timeout would hold 1460 s, over the 900 s any mark may hold."""
    world = _World(tmp_path)
    queued = world.submit("cand-a", queue_s=math.inf)

    with pytest.raises(LedgerError, match="transport_timeout_seconds=200"):
        world.evals("remove", entry_id(queued.key), call_seconds=200.0)

    assert world.questions == []
    assert world.status(queued) is SlurmJobStatus.PENDING
