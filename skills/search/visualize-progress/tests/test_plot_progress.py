"""Tests for plot_progress."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import plot_progress as pp


def record(commit: str, tput: float, ttft: float | None = None, time: str = "2026-01-01T00:00:00+00:00") -> str:
    """One passing measurement record as a JSON line."""
    r = {"commit": commit, "recorded_at": time, "metric": "tput", "direction": "max", "label": commit,
         "samples": [tput, tput * 1.01], "check": "pass"}
    if ttft is not None:
        r["observations"] = {"ttft_p50_ms": [ttft, ttft]}
    return json.dumps(r)


def make_state(root: Path, records: list[tuple[str, str]], statuses: tuple[str, ...] = ("won",)) -> Path:
    """Write a state directory with the given records and plan statuses."""
    (root / "measurements").mkdir(parents=True)
    (root / "plans").mkdir()
    for commit, line in records:
        (root / "measurements" / f"{commit}.jsonl").write_text(line + "\n")
    for i, status in enumerate(statuses):
        (root / "plans" / f"plan-{i:03d}-x.md").write_text(f"status: {status}\nclaim: x\n")
    return root


def test_load_points_pools_samples_and_skips_failed_checks(tmp_path: Path) -> None:
    """Samples pool per commit and failed checks are skipped."""
    failed = json.loads(record("b", 1.0))
    failed["check"] = "fail"
    state = make_state(tmp_path, [("a", record("a", 100, 50)), ("b", json.dumps(failed))])
    points, metric, direction = pp.load_points(state)
    assert [p.commit for p in points] == ["a"]
    assert (metric, direction) == ("tput", "max")
    assert abs(points[0].mean("tput") - 100.5) < 1e-9
    assert points[0].mean("ttft_p50_ms") == 50


def test_frontier_uses_noise_tolerance() -> None:
    """A point within the tolerance of a better one stays on the frontier."""

    def point(commit: str, tput: float, ttft: float) -> pp.Point:
        p = pp.Point(commit, "", "")
        p.values["tput"], p.values["ttft_p50_ms"] = [tput], [ttft]
        return p

    objs = {"tput": "max", "ttft_p50_ms": "min"}
    fast, slow, near = point("fast", 200, 100), point("slow", 100, 200), point("near", 199, 101)
    kept = {p.commit for p in pp.frontier([fast, slow, near], objs, 0.03)}
    assert kept == {"fast", "near"}  # near is within 3% of fast on both objectives


def test_cli_writes_all_outputs(tmp_path: Path) -> None:
    """The CLI writes every chart and the report."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a: str) -> str:
        argv = ["git", "-C", str(repo), *a]
        # lint-waiver: LW-715302 [S603]; a fixed git argv with no shell, building a throwaway repo.
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    first = git("rev-parse", "HEAD")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two")
    second = git("rev-parse", "HEAD")
    state = make_state(tmp_path / "state", [
        (first, record(first, 100, 50, "2026-01-01T00:00:00+00:00")),
        (second, record(second, 120, 40, "2026-01-02T00:00:00+00:00"))], statuses=("won", "rejected"))
    task = tmp_path / "task"
    task.mkdir()
    (task / "objectives.toml").write_text(
        '[[objective]]\nname = "tput"\ndirection = "max"\n[[objective]]\nname = "ttft_p50_ms"\n'
        'direction = "min"\n[pareto]\nrelative_noise = 0.03\n')
    out = tmp_path / "out"
    sys.argv = ["plot_progress", "--state", str(state), "--task-dir", str(task), "--branch", "main",
                "--repo", str(repo), "--out", str(out)]
    pp.main()
    for name in ("progress.png", "frontier.png", "latency.png", "plans.png", "report.md"):
        assert (out / name).exists()
