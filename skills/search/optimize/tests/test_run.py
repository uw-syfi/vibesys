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
measurements = importlib.import_module("measurements")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BASE, CANDIDATE = "a" * 40, "b" * 40


class Git:
    """Model only the repository operations offered by the lifecycle shell."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.head = BASE
        self.live: set[int] = set()

    def alive(self, pid: int) -> bool:
        return pid in self.live

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
        assert ledger.count("| implement |") == index + 1
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
        assert (
            run.call("track", "--id", "shell-1", "--description", "measurement", "--pid", "123")
            == 0
        )
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
    assert run.call("idle", "--reason", "no more ideas") == 0
    allowed = judge and gain == 10
    assert (run.call("move-main", "--id", "candidate", seconds=100) == 0) == allowed
    assert run.git.head == (CANDIDATE if allowed else BASE)
    if allowed:
        assert run.state()["idle_reason"] is None
        assert run.call("finish", seconds=1350) == 2
        run.record(CANDIDATE, [1980, 2000, 2020], 101)
        assert run.call("finish", seconds=1350) == 0
        assert '"ratio": 15.0' in (run.path / "report.md").read_text()


def test_early_finish_is_rejected(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.record(BASE, [10, 11, 12], 1)
    assert run.call("finish", seconds=1109) == 2
    assert (run.path.parent / "active").exists()


def test_historical_measurement_cannot_finalize_moved_main(tmp_path: Path) -> None:
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
        assert ledger.count("| implement |") == len(after["items"])
        for key, row in after["items"].items():
            assert (
                f"| {key} | implement | reduce copies | {row['state']} | {row['reason']} |"
                in ledger
            )


@given(early=st.booleans(), reason=st.sampled_from(["", " ", "no useful hypotheses"]))
def test_early_finish_requires_explicit_reason(
    tmp_path_factory: pytest.TempPathFactory, *, early: bool, reason: str
) -> None:
    run = Run(tmp_path_factory.mktemp("early"))
    run.record(BASE, [10, 11, 12], 1)
    args = ["finish", "--reason", reason]
    if early:
        args.append("--early")
    succeeds = early and bool(reason.strip())
    assert (run.call(*args, seconds=100) == 0) == succeeds
    if succeeds:
        assert reason in (run.path / "report.md").read_text()
        assert reason in (run.path / "ledger.md").read_text()


@given(live=st.booleans())
def test_untrack_checks_pid_before_deleting(
    tmp_path_factory: pytest.TempPathFactory, *, live: bool
) -> None:
    run = Run(tmp_path_factory.mktemp("process"))
    assert run.call("track", "--id", "timer", "--pid", "123", "--description", "sleep 165") == 0
    if live:
        run.git.live.add(123)
    before = run.state()
    assert (run.call("untrack", "--id", "timer") == 0) != live
    if live:
        assert run.state() == before


def test_running_sleep_cannot_be_untracked_or_finished(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.record(BASE, [10, 11, 12], 1)
    assert run.call("finish", seconds=1200) == 2
    assert run.call("track", "--id", "sleep", "--pid", "123", "--description", "sleep 165") == 0
    run.git.live.add(123)
    assert run.call("untrack", "--id", "sleep", seconds=1350) == 2
    assert run.call("finish", seconds=1350) == 2
    assert run.call("finish", "--early", "--reason", "done", seconds=1200) == 2
    run.git.live.remove(123)
    assert run.call("untrack", "--id", "sleep", seconds=1350) == 0
    assert run.call("finish", seconds=1350) == 0


@given(events=st.lists(st.sampled_from(["idle", "add", "returned", "abandoned"]), max_size=30))
def test_idle_reason_sequences_invalidate_only_on_return(
    tmp_path_factory: pytest.TempPathFactory, events: list[str]
) -> None:
    run = Run(tmp_path_factory.mktemp("idle"))
    expected = None
    for event in events:
        if event == "idle":
            assert run.call("idle", "--reason", "limited host capacity") == 0
            expected = "limited host capacity"
        elif event == "add":
            run.call("add", "--id", "x", "--kind", "implement", "--hypothesis", "test")
        else:
            code = run.call("transition", "--id", "x", "--to", event, "--reason", "done")
            if code == 0 and event == "returned":
                expected = None
        assert run.state()["idle_reason"] == expected
        assert controller.status(run.state(), NOW)["idle_reason"] == expected


def test_finish_reports_public_full_pool_comparison(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.record(BASE, [10, 11, 12], 1)
    run.record(CANDIDATE, [100, 101, 102], 2)
    state = run.state()
    state.update(main_commit=CANDIDATE, main_moved_at=(NOW + timedelta(seconds=3)).isoformat())
    (run.path / "run.json").write_text(json.dumps(state))
    run.git.head = CANDIDATE
    run.record(CANDIDATE, [200, 201, 202], 4)
    expected = measurements.compare_commit(measurements.load_records(run.path), CANDIDATE, BASE)
    assert run.call("finish", seconds=1350) == 0
    report = (run.path / "report.md").read_text()
    assert json.loads(report.split("```json\n")[1].split("\n```")[0]) == expected
    assert "[1, 2, 3]" in report


@pytest.mark.parametrize("pid", [0, 1, -1, 2**31, 99999999999999999999])
def test_invalid_pid_is_rejected_without_state_change(tmp_path: Path, pid: int) -> None:
    run = Run(tmp_path)
    before = run.state()
    assert run.call("track", "--id", "bad", "--description", "foreign", "--pid", str(pid)) == 2
    assert run.state() == before


def test_abort_releases_active_marker_and_records_reason(tmp_path: Path) -> None:
    run = Run(tmp_path)
    assert run.call("abort", "--reason", "lead crashed; manual recovery") == 0
    assert not (run.path.parent / "active").exists()
    assert run.state()["decisions"][-1]["kind"] == "abort"


def test_live_foreign_pid_can_be_forcibly_untracked_with_reason(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.git.live.add(123)
    assert run.call("track", "--id", "x", "--description", "owned command", "--pid", "123") == 0
    assert run.call("untrack", "--id", "x") == 2
    assert run.call("untrack", "--id", "x", "--force") == 2
    assert run.call("untrack", "--id", "x", "--force", "--reason", "PID reused") == 0
    assert run.state()["decisions"][-1]["kind"] == "force-untrack"


@given(moved=st.booleans())
def test_pending_promotion_recovers_at_either_git_boundary(
    tmp_path_factory: pytest.TempPathFactory, *, moved: bool
) -> None:
    run = Run(tmp_path_factory.mktemp("recovery"))

    class PromotionCrash(BaseException):
        pass

    class CrashingGit(Git):
        def __call__(self, cwd: Path, args: list[str]) -> str:
            if args[:2] == ["reset", "--hard"]:
                if moved:
                    super().__call__(cwd, args)
                raise PromotionCrash
            return super().__call__(cwd, args)

    run.record(BASE, [99, 100, 101], 1)
    run.record(CANDIDATE, [999, 1000, 1001], 2)
    assert (
        run.call("add", "--id", "candidate", "--kind", "implement", "--hypothesis", "faster") == 0
    )
    for target in ("returned", "judging"):
        assert run.call("transition", "--id", "candidate", "--to", target, "--reason", "done") == 0
    assert (
        run.call(
            "judge",
            "--id",
            "candidate",
            "--commit",
            CANDIDATE,
            "--result",
            "pass",
            "--reason",
            "checked",
        )
        == 0
    )
    assert run.call("transition", "--id", "candidate", "--to", "won", "--reason", "judged") == 0
    run.git = CrashingGit(run.git.root)
    with pytest.raises(PromotionCrash):
        run.call("move-main", "--id", "candidate")
    assert run.state()["pending_move"]["commit"] == CANDIDATE
    assert run.state()["main_commit"] == BASE
    assert run.call("status") == 0
    recovered = run.state()
    assert recovered["pending_move"] is None
    assert recovered["main_commit"] == (CANDIDATE if moved else BASE)
    assert len(recovered["promotions"]) == int(moved)
    assert run.call("status") == 0
    assert run.state() == recovered


@pytest.mark.parametrize("result", [None, False, True])
def test_judge_cannot_pass_missing_or_failed_declared_gate(
    tmp_path: Path, *, result: bool | None
) -> None:
    run = Run(tmp_path)
    task = tmp_path / ".vibesys/tasks/demo"
    task.mkdir(parents=True)
    declaration = "Latency SLA: latency must stay under the task limit."
    (task / "OBJECTIVE.md").write_text(declaration)
    run.record(BASE, [10, 11, 12], 1)
    run.record(CANDIDATE, [100, 110, 120], 2)
    path = run.path / "measurements.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row["gate_requirements"] = [declaration]
        row["gates"] = (
            {}
            if result is None
            else {declaration: {"passed": [result] * 3, "values": [0.1, 0.2, 0.3]}}
        )
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert run.call("add", "--id", "x", "--kind", "implement", "--hypothesis", "reduce copies") == 0
    assert run.call("transition", "--id", "x", "--to", "returned", "--reason", "done") == 0
    assert run.call("transition", "--id", "x", "--to", "judging", "--reason", "review") == 0
    assert (
        run.call(
            "judge", "--id", "x", "--commit", CANDIDATE, "--result", "pass", "--reason", "checked"
        )
        == 0
    ) == (result is True)
    comparison = measurements.compare_commit(measurements.load_records(run.path), CANDIDATE, BASE)
    assert comparison["gates"]["candidate"]["status"] == (
        "unmeasured" if result is None else "pass" if result else "fail"
    )
    if result is not None:
        assert comparison["gates"]["candidate"]["ranges"][declaration] == [0.1, 0.3]


def exercise_git_contract(git: controller.Git, root: Path, base: str, candidate: str) -> None:
    """The controller's supported Git operations, shared with the real-tier host test."""
    assert Path(git(root, ["rev-parse", "--show-toplevel"]).strip()) == root
    assert not git(root, ["status", "--porcelain"]).strip()
    destination = root / "worktree"
    git(root, ["worktree", "add", "-b", "contract", str(destination), base])
    assert git(destination, ["rev-parse", "HEAD"]).strip() == base
    git(destination, ["reset", "--hard", candidate])
    assert git(destination, ["rev-parse", "HEAD"]).strip() == candidate
    assert not git(destination, ["status", "--porcelain"]).strip()
    with pytest.raises(ValueError, match="not-a-git-verb"):
        git(destination, ["not-a-git-verb"])


def test_fake_git_obeys_controller_contract(tmp_path: Path) -> None:
    exercise_git_contract(Git(tmp_path), tmp_path, BASE, CANDIDATE)


def test_unmeasured_judge_releases_for_measurement_then_rejudge(tmp_path: Path) -> None:
    run = Run(tmp_path)
    assert run.call("add", "--id", "x", "--kind", "implement", "--hypothesis", "reduce copies") == 0
    for target in ("returned", "judging"):
        assert run.call("transition", "--id", "x", "--to", target, "--reason", "done") == 0
    assert (
        run.call(
            "judge",
            "--id",
            "x",
            "--commit",
            CANDIDATE,
            "--result",
            "unmeasured",
            "--reason",
            "missing gates",
        )
        == 0
    )
    assert not run.state()["items"]["x"]["judge_pass"]
    assert run.call("transition", "--id", "x", "--to", "won", "--reason", "claim") == 2
    assert run.call("transition", "--id", "x", "--to", "returned", "--reason", "measure next") == 0


def test_early_flag_after_wrap_up_does_not_log_early_completion(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.record(BASE, [10, 11, 12], 1)
    assert run.call("finish", "--early", "--reason", "work complete", seconds=1350) == 0
    assert run.state()["decisions"] == []


def test_legacy_run_can_be_closed_after_a_crash(tmp_path: Path) -> None:
    run = Run(tmp_path)
    state = run.state()
    del state["session_id"]
    (run.path / "run.json").write_text(json.dumps(state))
    assert run.call("abort", "--reason", "recover old lead") == 0
    assert run.state()["session_id"] is None
    assert not (run.path.parent / "active").exists()
