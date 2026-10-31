"""Public lifecycle API and CLI, with a faithful in-memory git boundary."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("run_controller", SCRIPTS / "run.py")
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BASE, CANDIDATE = "a" * 40, "b" * 40


class Git:
    """Model only the repository operations offered by the lifecycle shell."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.head = BASE

    def __call__(self, _cwd: Path, args: list[str]) -> str:
        if args == ["rev-parse", "--show-toplevel"]:
            return str(self.root)
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return str(self.root / ".git/info/exclude")
        if args == ["rev-parse", "HEAD"]:
            return self.head
        if args[0] == "status":
            return ""
        if args[:2] == ["worktree", "add"]:
            Path(args[-2]).mkdir(parents=True)
            return ""
        if args[:2] == ["reset", "--hard"]:
            self.head = args[2]
            return ""
        raise ValueError(args)


class Run:
    def __init__(self, root: Path) -> None:
        self.git = Git(root)
        out, err = io.StringIO(), io.StringIO()
        assert (
            controller.main(
                ["start", "--task", "demo", "--seconds", "1800"],
                now=NOW,
                out=out,
                err=err,
                git=self.git,
            )
            == 0
        ), err.getvalue()
        self.path = Path(out.getvalue().strip())

    def call(self, *args: str, seconds: int = 0) -> int:
        return controller.main(
            [args[0], "--run", str(self.path), *args[1:]],
            now=NOW + timedelta(seconds=seconds),
            out=io.StringIO(),
            err=io.StringIO(),
            git=self.git,
        )

    def state(self) -> dict[str, Any]:
        return json.loads((self.path / "run.json").read_text())

    def record(self, commit: str, samples: list[float], seconds: int) -> None:
        record = {
            "schema": 1,
            "recorded_at": (NOW + timedelta(seconds=seconds)).isoformat(),
            "host": "host-a",
            "commit": commit,
            "role": "base" if commit == BASE else "candidate",
            "label": "sample",
            "metric": "throughput",
            "direction": "max",
            "command": "benchmark --port 7101",
            "samples": samples,
            "check": "pass",
            "exclusive": "yes",
            "evaluator": "e" * 64,
        }
        with (self.path / "measurements.jsonl").open("a") as stream:
            stream.write(json.dumps(record) + "\n")


@given(paths=st.lists(st.sampled_from(["abandoned", "parked", "won"]), min_size=1, max_size=15))
def test_legal_sequences_keep_rendered_ledger_consistent(
    tmp_path_factory: pytest.TempPathFactory, paths: list[str]
) -> None:
    run = Run(tmp_path_factory.mktemp("run"))
    for index, target in enumerate(paths):
        item = f"item-{index}"
        assert (
            run.call("add", "--id", item, "--kind", "implement", "--hypothesis", "reduce copies")
            == 0
        )
        assert run.call("transition", "--id", item, "--to", "returned", "--reason", "done") == 0
        assert (
            run.call("transition", "--id", item, "--to", "judging", "--reason", "independent check")
            == 0
        )
        assert (
            run.call(
                "judge",
                "--id",
                item,
                "--commit",
                CANDIDATE,
                "--result",
                "pass",
                "--reason",
                "own checker passed on port 7101",
            )
            == 0
        )
        assert run.call("transition", "--id", item, "--to", target, "--reason", "evidence") == 0
        ledger = (run.path / "ledger.md").read_text()
        assert len(ledger.splitlines()) == index + 3
        assert f"| {target} |" in ledger
        assert controller.status(run.state(), NOW)["free_slots"] == 3


@given(early=st.booleans(), opened=st.booleans(), fresh=st.booleans(), commands=st.booleans())
def test_finish_succeeds_iff_preconditions_hold(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    early: bool,
    opened: bool,
    fresh: bool,
    commands: bool,
) -> None:
    run = Run(tmp_path_factory.mktemp("run"))
    run.record(BASE, [10, 11, 12], 1 if fresh else 0)
    if opened:
        assert run.call("add", "--id", "open", "--kind", "profile", "--hypothesis", "inspect") == 0
    if commands:
        assert run.call("track", "--id", "shell-1", "--description", "measurement") == 0
    succeeds = not early and not opened and fresh and not commands
    assert (run.call("finish", seconds=1109 if early else 1350) == 0) == succeeds
    assert (run.path.parent / "active").exists() != succeeds
    if succeeds:
        assert '"ratio": 1.0' in (run.path / "report.md").read_text()


@given(gain=st.sampled_from([1.0, 10.0, 0.1]), judge=st.booleans())
def test_main_never_moves_without_better_and_judge_pass(
    tmp_path_factory: pytest.TempPathFactory, gain: float, *, judge: bool
) -> None:
    run = Run(tmp_path_factory.mktemp("run"))
    run.record(BASE, [99, 100, 101], 1)
    run.record(CANDIDATE, [99 * gain, 100 * gain, 101 * gain], 2)
    assert (
        run.call("add", "--id", "candidate", "--kind", "implement", "--hypothesis", "faster") == 0
    )
    for state in ("returned", "judging"):
        assert run.call("transition", "--id", "candidate", "--to", state, "--reason", "done") == 0
    assert (
        run.call(
            "judge",
            "--id",
            "candidate",
            "--commit",
            CANDIDATE,
            "--result",
            "pass" if judge else "fail",
            "--reason",
            "own checker",
        )
        == 0
    )
    assert (
        run.call("transition", "--id", "candidate", "--to", "won", "--reason", "judged") == 0
    ) == judge
    allowed = judge and gain == 10
    assert (run.call("move-main", "--id", "candidate", seconds=100) == 0) == allowed
    assert run.git.head == (CANDIDATE if allowed else BASE)
    if allowed:
        assert run.call("finish", seconds=1350) == 2
        run.record(CANDIDATE, [1980, 2000, 2020], 101)
        assert run.call("finish", seconds=1350) == 0
        assert '"ratio": 20.0' in (run.path / "report.md").read_text()


def test_trial_two_early_finish_is_rejected(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.record(BASE, [10, 11, 12], 1)
    assert run.call("finish", seconds=1109) == 2
    assert (run.path.parent / "active").exists()


def test_trial_two_historical_measurement_cannot_finalize_moved_main(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.git.head = CANDIDATE
    run.record(BASE, [10, 11, 12], 1)
    run.record(CANDIDATE, [100, 101, 102], 2)
    assert run.call("finish", seconds=1350) == 2
    assert (run.path.parent / "active").exists()


@given(target=st.sampled_from(["dispatched", "returned", "judging", "won", "parked", "abandoned"]))
def test_transition_rejects_illegal_edges_without_mutating_input(
    tmp_path_factory: pytest.TempPathFactory, target: str
) -> None:
    run = Run(tmp_path_factory.mktemp("run"))
    assert run.call("add", "--id", "item", "--kind", "profile", "--hypothesis", "inspect") == 0
    before = run.state()
    if target in {"returned", "abandoned"}:
        after = controller.transition(before, "item", target, "done")
        assert after["items"]["item"]["state"] == target
    else:
        with pytest.raises(ValueError, match="invalid transition"):
            controller.transition(before, "item", target, "done")
    assert run.state() == before
    assert before["items"]["item"]["state"] == "dispatched"


@given(
    events=st.lists(
        st.tuples(
            st.sampled_from(["add", "returned", "judging", "parked", "abandoned"]),
            st.integers(min_value=0, max_value=5),
        ),
        max_size=40,
    )
)
def test_interleaved_work_sequences_preserve_ledger_and_slot_invariants(
    tmp_path_factory: pytest.TempPathFactory, events: list[tuple[str, int]]
) -> None:
    run = Run(tmp_path_factory.mktemp("run"))
    for action, index in events:
        item = f"item-{index}"
        before = run.state()
        if action == "add":
            code = run.call(
                "add", "--id", item, "--kind", "implement", "--hypothesis", "reduce copies"
            )
        else:
            code = run.call("transition", "--id", item, "--to", action, "--reason", "evidence")
        after = run.state()
        if code:
            assert after == before
        opened = controller.status(after, NOW)["open_items"]
        busy = sum(row["state"] in {"dispatched", "judging"} for row in opened)
        assert 0 <= busy <= after["fanout"]
        assert controller.status(after, NOW)["free_slots"] == after["fanout"] - busy
        ledger = (run.path / "ledger.md").read_text()
        assert len(ledger.splitlines()) == len(after["items"]) + 2
        for key, row in after["items"].items():
            assert (
                f"| {key} | implement | reduce copies | {row['state']} | {row['reason']} |"
                in ledger
            )
