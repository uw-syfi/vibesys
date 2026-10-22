"""Adoption publishes the winner before optionally advancing a clean run checkout."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from typing import TYPE_CHECKING, Literal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.session_world import SettledRunInvocations
from tests.support.workspace_world import open_workspace_env

from vs_core.api import (
    AdoptRevision,
    DiscardWorkspace,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    RequestId,
    RetainedCandidate,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
    SettlementId,
    TrustedBaseline,
    VerifyAdoption,
)
from vs_evaluation.api import (
    ContentDigest,
    EvidenceFingerprints,
    EvidenceMetric,
    EvidenceOutcome,
    TrustedEvidence,
)
from vs_evaluation.api import EvidenceKind as StageKind
from vs_project.api import GitTracker, NullGitTrackerEvents, adoption_branch
from vs_project.api.testing import FakeGitRepositories, GitKind
from vs_runtime.api.core import (
    ReceiptEvidenceLedger,
    ReceiptStore,
    RuntimeWorkspaceRequests,
    adoption_commit_message,
    commit_of,
)
from vs_sim.api.testing import InlineBlockingRunner, SimThreads, examples

from .test_workspace_requests import (
    _TREE,
    _attempt,
    _common,
    _ensure,
    _run,
    _snapshot_of,
    _status,
    _visible_files,
)

if TYPE_CHECKING:
    import pytest

    from vs_project.api.git_repository import GitRepository
    from vs_runtime.api.infrastructure import RuntimeWorkspaces

_DIGEST = "a" * 64
_SCOPE = Scope(owner=RunId(root="run-1"), generation=0)
_SETTLEMENT = SettlementId(root="settlement:000002/settle.1")

_METRIC = st.builds(
    EvidenceMetric,
    name=st.sampled_from(("throughput", "p99_latency", "ops")),
    value=st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    direction=st.sampled_from(("max", "min", None)),
    unit=st.sampled_from(("ops/s", "ms", None)),
)
_MEASUREMENT = st.tuples(
    st.lists(_METRIC, min_size=1, max_size=3),
    st.sampled_from(("official", "baseline")),
    st.booleans(),
)
"""Metrics, the purpose they were measured for, and whether the ledger recorded them."""


def _executor(workspaces: RuntimeWorkspaces, store: ReceiptStore) -> RuntimeWorkspaceRequests:
    return RuntimeWorkspaceRequests(
        workspaces,
        store,
        SettledRunInvocations(),
        blocking=InlineBlockingRunner(),
        evidence=ReceiptEvidenceLedger(store),
    )


type _Measurement = tuple[list[EvidenceMetric], Literal["official", "baseline"], bool]


def _measured(
    ledger: ReceiptEvidenceLedger, index: int, revision: RevisionRef, measurement: _Measurement
) -> EvidenceRef:
    metrics, purpose, recorded = measurement
    digest = ContentDigest(value=_DIGEST)
    evidence = TrustedEvidence(
        evidence_id=hashlib.sha256(str(index).encode()).hexdigest(),
        evaluation_id="evaluation",
        stage_name="benchmark",
        kind=StageKind.BENCHMARK,
        fingerprints=EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
        trusted_inputs=digest,
        outcome=EvidenceOutcome.PASSED,
        metrics=tuple(metrics),
        accepted_round=0,
    )
    source = RequestId(root=f"measure-{index}")
    if recorded:
        ledger.record(source, evidence, purpose)
    return EvidenceRef(
        evidence_id=EvidenceId(root=evidence.evidence_id),
        kind=EvidenceKind.BENCHMARK,
        purpose=purpose,
        scope=_SCOPE,
        source_request=source,
        candidate=revision,
        observation_sequence=0,
        evaluator_digest=_DIGEST,
        workload_digest=_DIGEST,
        environment_digest=_DIGEST,
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )


def _expected_lines(metrics: list[EvidenceMetric]) -> list[str]:
    return [f"{metric.name} = {metric.value:.6g}" for metric in metrics]


def _branches(repository: GitRepository) -> dict[str, str | None]:
    return {name: repository.resolve_commit(name) for name in repository.branches("")}


def _dirty(repository: GitRepository) -> list[str]:
    paths = (
        *repository.uncommitted_paths(()),
        *repository.unversioned_paths(include_ignored=False),
    )
    return sorted(path for path in paths if not path.startswith(".vibesys/"))


@settings(
    max_examples=examples(pr=3, nightly=12),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(tree=_TREE, measurements=st.lists(_MEASUREMENT, max_size=3))
def test_an_adopted_winner_is_committed_on_the_run_branch_and_the_next_run_starts(
    tmp_path_factory: pytest.TempPathFactory,
    tree: dict[str, str],
    measurements: list[_Measurement],
) -> None:
    base = tmp_path_factory.mktemp("adopt")

    async def exercise(
        workspaces: RuntimeWorkspaces,
        store: ReceiptStore,
        repository: GitRepository,
        disk: FakeGitRepositories,
    ) -> None:
        executor = _executor(workspaces, store)
        attempt = _attempt()
        worktrees = set(disk.worktrees(workspaces.root.path))
        ensured = await _run(executor, _ensure(workspaces, attempt, "e"))
        assert _status(ensured) is ObservationStatus.SUCCEEDED
        (path,) = set(disk.worktrees(workspaces.root.path)) - worktrees
        for child in path.iterdir():
            if child.name not in {".git", ".vibesys"}:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        for name, text in tree.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(text, encoding="utf-8")
        revision = await _snapshot_of(executor, attempt, "s")
        # Core ends the attempt, and its workspace goes, before it adopts the winner.
        discard = DiscardWorkspace(**_common(attempt, "d"), attempt=attempt)
        assert _status(await _run(executor, discard)) is ObservationStatus.SUCCEEDED
        ledger = ReceiptEvidenceLedger(store)
        evidence = tuple(
            _measured(ledger, index, revision, measurement)
            for index, measurement in enumerate(measurements)
        )
        selection = RetainedCandidate(settlement_id=_SETTLEMENT, revision=revision)
        branch = repository.current_branch()
        before = _branches(repository)

        adopt = AdoptRevision(
            request_id=RequestId(root="adopt-1"),
            scope=_SCOPE,
            selection=selection,
            evidence=evidence,
        )
        assert _status(await _run(executor, adopt)) is ObservationStatus.SUCCEEDED

        assert _visible_files(workspaces.root.path) == tree
        assert _dirty(repository) == []
        after = _branches(repository)
        assert {name for name in after if after[name] != before.get(name)} <= {
            branch,
            adoption_branch("workspace-requests"),
        }
        commit = commit_of(revision)
        assert commit is not None
        message = adoption_commit_message(adopt, commit, ledger)
        assert repository.recent_subjects(1)[0].subject == message.splitlines()[0]

        verify = VerifyAdoption(
            request_id=RequestId(root="verify-1"), scope=_SCOPE, selection=selection
        )
        assert _status(await _run(executor, verify)) is ObservationStatus.SUCCEEDED
        # A repeated adoption (a retry under a new identity) finds nothing to commit.
        head = repository.head()
        again = adopt.model_copy(update={"request_id": RequestId(root="adopt-2")})
        assert _status(await _run(executor, again)) is ObservationStatus.SUCCEEDED
        assert repository.head() == head

    with open_workspace_env(base, git=GitKind.FAKE) as env:
        disk = env.git_disk
        assert isinstance(disk, FakeGitRepositories)
        project = env.project.root
        try:
            asyncio.run(
                exercise(
                    env.hosts[0],
                    ReceiptStore(env.receipts_namespace(), threads=SimThreads()),
                    disk.repository(project),
                    disk,
                )
            )
        finally:
            for host in reversed(env.hosts):
                asyncio.run(host.close())

        # The next run is a new process over the same project.
        following = disk.repository(project)
        tracker = GitTracker(
            project, run_id="next-run", events=NullGitTrackerEvents(), repository=following
        )
        tracker.init(existing=False)
        assert following.current_branch() == "vibesys-runs/next-run"


@settings(
    max_examples=examples(pr=10, nightly=100),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    measurements=st.lists(_MEASUREMENT, max_size=4),
    commit=st.text(alphabet="0123456789abcdef", min_size=40, max_size=40),
    kept=st.sampled_from(("candidate", "input")),
)
def test_the_adoption_message_names_the_revision_and_every_recorded_metric(
    tmp_path_factory: pytest.TempPathFactory,
    measurements: list[_Measurement],
    commit: str,
    kept: Literal["candidate", "input"],
) -> None:
    with open_workspace_env(tmp_path_factory.mktemp("message")) as env:
        ledger = ReceiptEvidenceLedger(ReceiptStore(env.receipts_namespace(), threads=SimThreads()))
        revision = RevisionRef(revision_id=RevisionId(root=commit), digest=f"git-commit:{commit}")
        evidence = tuple(
            _measured(ledger, index, revision, measurement)
            for index, measurement in enumerate(measurements)
        )
        selection = (
            TrustedBaseline(revision=revision)
            if kept == "input"
            else RetainedCandidate(settlement_id=_SETTLEMENT, revision=revision)
        )
        adopt = AdoptRevision(
            request_id=RequestId(root="adopt"), scope=_SCOPE, selection=selection, evidence=evidence
        )
        lines = adoption_commit_message(adopt, commit, ledger).splitlines()
        for host in reversed(env.hosts):
            asyncio.run(host.close())

    assert lines[0] == (
        f"vibesys: keep the input revision ({commit[:12]})"
        if kept == "input"
        else f"vibesys: adopt candidate {_SETTLEMENT.root} ({commit[:12]})"
    )
    assert lines[1:3] == ["", f"Revision: {commit}"]
    # Every recorded metric is named, in order; one the ledger never recorded is left out.
    recorded = [
        metric for metrics, _purpose, logged in measurements if logged for metric in metrics
    ]
    measured = lines[3:]
    assert len(measured) == len(recorded)
    for line, expected in zip(measured, _expected_lines(recorded), strict=True):
        assert line.startswith("Measured (")
        assert expected in line
