"""An adopted winner is committed on the run's branch, so the next run can start.

Adoption makes the root workspace hold the winner. Left uncommitted, that tree makes the
project dirty, and the next run refuses to start ("existing project repository must be
clean"). The adoption therefore commits it on the run's own branch, with a message that
names the candidate and what it measured, and touches no other branch. The next run is a
new process, so these tests start it in a copy of the project, as
``test_git_tracker_next_run`` does.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from typing import TYPE_CHECKING, Literal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.real_git import REAL_GIT, real_git_tracker
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
    RunId,
    Scope,
    SettlementId,
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
from vs_project.api import NullGitTrackerEvents
from vs_project.api.testing import GitKind
from vs_runtime.api.core import (
    ReceiptEvidenceLedger,
    ReceiptStore,
    RuntimeWorkspaceRequests,
    commit_of,
)
from vs_sim.api.testing import InlineBlockingRunner, SimThreads, examples

from .test_workspace_requests import (
    _TREE,
    _attempt,
    _common,
    _ensure_at,
    _run,
    _snapshot_of,
    _status,
    _visible_files,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_core.api import RevisionRef
    from vs_runtime.api.core import RuntimeWorkspaces

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


def _git(root: Path, *args: str) -> str:
    result = REAL_GIT.run(list(args), cwd=root)
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


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


def _branches(root: Path) -> dict[str, str]:
    lines = _git(root, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    return dict(line.split(" ", 1) for line in lines.splitlines())


def _dirty(root: Path) -> list[str]:
    status = _git(root, "status", "--porcelain", "--untracked-files=all", "--", ".")
    return [line for line in status.splitlines() if ".vibesys/" not in line]


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

    async def exercise(workspaces: RuntimeWorkspaces, store: ReceiptStore) -> None:
        executor = _executor(workspaces, store)
        root = workspaces.root.path
        attempt = _attempt()
        path = await _ensure_at(executor, workspaces, attempt, "e")
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
        branch = _git(root, "branch", "--show-current")
        before = _branches(root)

        adopt = AdoptRevision(
            request_id=RequestId(root="adopt-1"),
            scope=_SCOPE,
            selection=selection,
            evidence=evidence,
        )
        assert _status(await _run(executor, adopt)) is ObservationStatus.SUCCEEDED

        assert _visible_files(root) == tree
        assert _dirty(root) == []
        after = _branches(root)
        assert {name for name in after if after[name] != before.get(name)} <= {branch}
        message = _git(root, "log", "-1", "--format=%B")
        commit = commit_of(revision)
        assert commit is not None
        assert message.splitlines()[0] == (
            f"vibesys: adopt candidate {_SETTLEMENT.root} ({commit[:12]})"
        )
        assert f"Revision: {commit}" in message
        # Every recorded metric is named; a measurement the ledger never recorded is left out.
        recorded_metrics = [
            metric for metrics, _purpose, recorded in measurements if recorded for metric in metrics
        ]
        measured = [line for line in message.splitlines() if line.startswith("Measured (")]
        assert len(measured) == len(recorded_metrics)
        for line, expected in zip(measured, _expected_lines(recorded_metrics), strict=True):
            assert expected in line

        verify = VerifyAdoption(
            request_id=RequestId(root="verify-1"), scope=_SCOPE, selection=selection
        )
        assert _status(await _run(executor, verify)) is ObservationStatus.SUCCEEDED
        # A repeated adoption (a retry under a new identity) finds nothing to commit.
        head = _git(root, "rev-parse", "HEAD")
        again = adopt.model_copy(update={"request_id": RequestId(root="adopt-2")})
        assert _status(await _run(executor, again)) is ObservationStatus.SUCCEEDED
        assert _git(root, "rev-parse", "HEAD") == head

    with open_workspace_env(base, git=GitKind.REAL) as env:
        try:
            asyncio.run(
                exercise(
                    env.hosts[0],
                    ReceiptStore(env.receipts_namespace(), threads=SimThreads()),
                )
            )
        finally:
            for host in reversed(env.hosts):
                asyncio.run(host.close())
        project = env.project.root

    following = shutil.copytree(project, base / "next", symlinks=True)
    tracker = real_git_tracker(following, run_id="next-run", events=NullGitTrackerEvents())
    tracker.init(existing=False)
    assert _git(following, "branch", "--show-current") == "vibesys-runs/next-run"
