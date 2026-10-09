"""Remote commands per Slurm operation are bounded, because each costs a transport round trip.

A real cluster answers one command in about 1.2 seconds, so every command removed from the
submit, poll or collect path shortens each evaluation's feedback loop by about that much.
The budgets below are the current counts; a change that adds a command must raise them
on purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_slurm.api import (
    REQUESTS_FILE,
    FakeConnector,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmCluster,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRunner,
    SlurmJobStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

_SUBMIT_BUDGET = 19
# A poll of a computing multi-stage batch also reads how many stages have finished.
_ACTIVE_POLL_BUDGET = 3
_TERMINAL_POLL_BUDGET = 3
_COLLECT_BUDGET = 6


def _recorded(connector: FakeConnector) -> int:
    path = connector.state / REQUESTS_FILE
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_submit_poll_and_collect_stay_within_their_command_budgets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x")
    support = tmp_path / "support"
    support.mkdir()
    (support / "s.txt").write_text("y")
    remote = tmp_path / "remote"
    remote.mkdir()
    connector = FakeConnector(tmp_path / "connector")
    runner = SlurmJobRunner(
        SlurmConfig(
            name="budget",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        process=connector,
        clock=lambda: 0.0,
    )
    cluster = SlurmCluster(runner, state_root=tmp_path / "ids")
    connector.script(
        "op",
        states=(SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED, SlurmJobStatus.COMPLETED),
    )
    request = SlurmBatchRequest(
        workspace=workspace,
        stages=(
            SlurmBatchStage(name="accuracy", command=("true",)),
            SlurmBatchStage(name="benchmark", command=("true",)),
        ),
        support_trees={"support": support},
    )
    counts: dict[str, int] = {}
    mark = 0

    def measure(label: str) -> None:
        nonlocal mark
        total = _recorded(connector)
        counts[label] = total - mark
        mark = total

    outcome = cluster.submit(request, operation_id="op")
    measure("submit")
    cluster.inspect("op")
    measure("active poll")
    cluster.inspect("op")
    measure("terminal poll")
    cluster.collect("op")
    measure("collect")
    assert outcome.operation_id == "op"
    assert counts["submit"] <= _SUBMIT_BUDGET, counts
    assert counts["active poll"] <= _ACTIVE_POLL_BUDGET, counts
    assert counts["terminal poll"] <= _TERMINAL_POLL_BUDGET, counts
    assert counts["collect"] <= _COLLECT_BUDGET, counts
