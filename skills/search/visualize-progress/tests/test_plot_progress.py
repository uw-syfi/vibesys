"""Tests for plot_progress, driven by protojson fixtures in the shape `vibesys-cli --json` prints."""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import plot_progress as pp

OBJECTIVES = {
    "metrics": [
        {"name": "tput", "direction": "DIRECTION_MAXIMIZE"},
        {"name": "ttft_p50_ms", "direction": "DIRECTION_MINIMIZE"},
    ],
    "relative_noise": 0.03,
}


def measurement(
    commit: str, job: str, tput: float, ttft: float | None, objectives: dict | None
) -> dict:
    """One `Measurement` message as protojson prints it."""
    measure = {
        "metric": {"name": "tput", "direction": "DIRECTION_MAXIMIZE"},
        "value": tput,
        "samples": [tput - 1, tput + 1],
    }
    if ttft is not None:
        measure["observations"] = [
            {"name": "ttft_p50_ms", "value": ttft, "values": [ttft - 1, ttft + 1]}
        ]
    if objectives:
        measure["objectives"] = objectives
    return {"commit": commit, "job_ids": [job], "measure": measure}


class FakeCli:
    """Answers the three commands the script runs from fixed protojson, as the CLI would."""

    def __init__(
        self,
        commits: list[tuple[str, float, float | None, str]],
        plans: tuple[str, ...] = ("PLAN_STATUS_WON",),
        objectives: dict | None = OBJECTIVES,
    ) -> None:
        self.commits, self.plans, self.objectives = commits, plans, objectives

    def __call__(self, args: Sequence[str]) -> str:
        if args[0] == "jobs":
            return json.dumps(
                {
                    "jobs": [
                        {"id": f"j-{c}", "spec": {"commit": c}, "notes": [{"text": f"try {c[:3]}"}]}
                        for c, *_ in self.commits
                    ]
                }
            )
        if args[0] == "show":
            commit = args[-1]
            _, tput, ttft, when = next(c for c in self.commits if c[0] == commit)
            return json.dumps(
                {
                    "results": [{"job_id": f"j-{commit}", "recorded_at": when}],
                    "measurements": [
                        measurement(commit, f"j-{commit}", tput, ttft, self.objectives)
                    ],
                }
            )
        if list(args[:2]) == ["plan", "list"]:
            return json.dumps({"plans": [{"status": s} for s in self.plans]})
        raise AssertionError(args)


def test_load_points_uses_the_clis_numbers_and_orders_by_time() -> None:
    """Headline values come from the CLI's reply, in time order, with its objectives and noise."""
    cli = FakeCli(
        [("b" * 40, 120, 40, "2026-01-02T00:00:00Z"), ("a" * 40, 100, 50, "2026-01-01T00:00:00Z")]
    )
    points, hl = pp.load_points(cli)
    assert [p.commit[0] for p in points] == ["a", "b"]
    assert points[0].mean("tput") == 100  # the reported value, not our own average
    assert points[0].mean("ttft_p50_ms") == 50
    assert points[0].span("tput") == (99, 101)
    assert hl is not None
    assert (hl.metric, hl.direction) == ("tput", "max")
    assert hl.objs == {"tput": "max", "ttft_p50_ms": "min"}
    assert hl.tols == {"tput": 0.03, "ttft_p50_ms": 0.03}


def test_no_measured_commit_is_an_error(tmp_path: Path) -> None:
    """An optimization the CLI reports nothing measured for is not plotted."""
    with pytest.raises(SystemExit, match="nothing to plot"):
        pp.build(FakeCli([]), [], tmp_path)


def test_frontier_uses_each_objectives_noise_tolerance() -> None:
    """A point within the tolerance of a better one stays on the frontier."""

    def point(commit: str, tput: float, ttft: float) -> pp.Point:
        p = pp.Point(commit, pp.moment("2026-01-01T00:00:00+00:00"), "")
        p.values["tput"], p.values["ttft_p50_ms"] = [tput], [ttft]
        return p

    objs = {"tput": "max", "ttft_p50_ms": "min"}
    fast, slow, near = point("fast", 200, 100), point("slow", 100, 200), point("near", 199, 101)
    kept = {
        p.commit for p in pp.frontier([fast, slow, near], objs, {"tput": 0.03, "ttft_p50_ms": 0.03})
    }
    assert kept == {"fast", "near"}  # near is within 3% of fast on both objectives


def test_plan_statuses_come_from_the_enum_names() -> None:
    """`PLAN_STATUS_WON` is the status `won`."""
    assert pp.read_plan_statuses(
        FakeCli([], plans=("PLAN_STATUS_WON", "PLAN_STATUS_REJECTED"))
    ) == ["won", "rejected"]


def test_build_writes_all_outputs(tmp_path: Path) -> None:
    """A task with several objectives gets every chart and the report."""
    first, second = "a" * 40, "b" * 40
    cli = FakeCli(
        [(first, 100, 50, "2026-01-01T00:00:00Z"), (second, 120, 40, "2026-01-02T00:00:00Z")],
        plans=("PLAN_STATUS_WON", "PLAN_STATUS_REJECTED"),
    )
    pp.build(cli, [first, second], tmp_path)
    for name in ("progress.png", "frontier.png", "latency.png", "plans.png", "report.md"):
        assert (tmp_path / name).exists()


def test_a_single_metric_task_skips_the_charts_that_need_a_second_objective(tmp_path: Path) -> None:
    """Without `objectives` there is no frontier or latency chart, and the report links only real images."""
    first = "a" * 40
    pp.build(
        FakeCli([(first, 100, None, "2026-01-01T00:00:00Z")], objectives=None), [first], tmp_path
    )
    assert (tmp_path / "progress.png").exists()
    assert not (tmp_path / "frontier.png").exists()
    assert not (tmp_path / "latency.png").exists()
    assert "frontier.png" not in (tmp_path / "report.md").read_text()
