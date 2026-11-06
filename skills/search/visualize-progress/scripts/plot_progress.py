# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.8"]
# ///
"""Plot an optimization's progress from what vibesys-cli reports.

Reads the `--json` output of `vibesys-cli jobs`, `show` and `plan list` (the CLI is run for you; its
messages are the contract) and the result branch's history from git, and writes to `--out`:

  progress.png  every experiment in run order, with the best so far as a line
  frontier.png  headline versus each latency objective, with the Pareto frontier
  latency.png   each latency objective along the result branch
  plans.png     plans by outcome
  report.md     the promoted heads as a table, and the images

Nothing is repaired: a reply the CLI should not send is an error. A commit's point is its newest
measurement (one environment, one task version), with the headline numbers the CLI computed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from matplotlib.figure import Figure

    #: Runs vibesys-cli with these arguments and returns its standard output.
    CliRun = Callable[[Sequence[str]], str]


Theme = dict[str, str]
THEMES: dict[str, Theme] = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#8a8984",
        "grid": "#e6e5e1",
        "blue": "#2a78d6",
        "orange": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#8a8984",
        "grid": "#2e2e2c",
        "blue": "#3987e5",
        "orange": "#d95926",
    },
}
STATUSES = ["won", "chosen", "open", "parked", "rejected"]
ENUM_PREFIXES = {"direction": "DIRECTION_", "status": "PLAN_STATUS_"}


@dataclass
class Point:
    """One commit's newest measurement."""

    commit: str
    time: datetime
    label: str
    values: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    #: The headline number of each series as the CLI computed it; a series without one (an older
    #: daemon) falls back to the mean of its samples.
    reported: dict[str, float] = field(default_factory=dict)
    on_main: bool = False
    order: int = -1

    def mean(self, name: str) -> float | None:
        """The CLI's number for `name`, else the mean of its samples, or None if unmeasured."""
        if name in self.reported:
            return self.reported[name]
        v = self.values.get(name)
        return sum(v) / len(v) if v else None

    def span(self, name: str) -> tuple[float, float]:
        """Min and max of the samples for `name`."""
        return min(self.values[name]), max(self.values[name])


@dataclass
class Headline:
    """The headline metric, its direction, every objective's direction and noise tolerance."""

    metric: str
    direction: str
    objs: dict[str, str]
    tols: dict[str, float]


def direction_of(name: str) -> str:
    """`max` or `min` from a protojson `Direction` name."""
    return {"DIRECTION_MAXIMIZE": "max", "DIRECTION_MINIMIZE": "min"}[name]


def moment(text: str) -> datetime:
    """A protojson timestamp."""
    return datetime.fromisoformat(text)


def pretty(name: str) -> str:
    """Axis label for a metric name."""
    return name.replace("_", " ").capitalize()


def load_points(run: CliRun) -> tuple[list[Point], Headline | None]:
    """One point per measured commit, by time, and the headline of the newest.

    `run` is vibesys-cli. The commits are the ones with a measure job; `show` gives each its
    measurements, and the newest is the point. The headline's objectives come from that
    measurement; a single-metric task has only its metric.
    """
    jobs = json.loads(run(["jobs", "--json", "--kind", "measure"])).get("jobs", [])
    labels: dict[str, str] = {}
    for job in jobs:
        notes = job.get("notes", [])
        labels.setdefault(job["spec"]["commit"], notes[0]["text"] if notes else "")
    points: list[Point] = []
    newest: dict | None = None
    for commit, label in labels.items():
        shown = json.loads(run(["show", "--json", commit]))
        measurements = shown.get("measurements", [])
        if not measurements:
            continue
        chosen = measurements[-1]
        measure = chosen["measure"]
        taken = [
            moment(r["recorded_at"])
            for r in shown.get("results", [])
            if r.get("job_id") in chosen.get("job_ids", []) and "recorded_at" in r
        ]
        metric = measure["metric"]["name"]
        point = Point(commit, min(taken), label or commit[:7])
        point.values[metric] += measure.get("samples", [])
        point.reported[metric] = measure["value"]
        for obs in measure.get("observations", []):
            point.values[obs["name"]] += obs.get("values", [])
            if "value" in obs:
                point.reported[obs["name"]] = obs["value"]
        points.append(point)
        if newest is None or point.time >= max(p.time for p in points):
            newest = measure
    points.sort(key=lambda p: p.time)
    if newest is None:
        return points, None
    metric = newest["metric"]["name"]
    direction = direction_of(newest["metric"]["direction"])
    spec = newest.get("objectives")
    objs = (
        {m["name"]: direction_of(m["direction"]) for m in spec["metrics"]}
        if spec
        else {metric: direction}
    )
    base = spec.get("relative_noise", 0.0) if spec else 0.0
    own = {n["name"]: n.get("relative_noise", 0.0) for n in spec.get("noise", [])} if spec else {}
    return points, Headline(metric, direction, objs, {n: own.get(n, base) for n in objs})


def branch_commits(repo: Path, branch: str) -> list[str]:
    """Full SHAs on the result branch, oldest first."""
    argv = ["git", "-C", str(repo), "rev-list", "--reverse", branch]
    # lint-waiver: LW-715301 [S603]; a fixed git argv with no shell; reading history through git is the point.
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.split()  # noqa: S603


def mark_main(points: list[Point], shas: list[str]) -> None:
    """Mark the points whose commit is on the result branch, in history order."""
    rank = {c: i for i, c in enumerate(shas)}
    for p in points:
        if p.commit in rank:
            p.on_main, p.order = True, rank[p.commit]


def read_plan_statuses(run: CliRun) -> list[str]:
    """The status of every plan `vibesys-cli plan list` reports, lower case."""
    plans = json.loads(run(["plan", "list", "--json"])).get("plans", [])
    return [p["status"].removeprefix(ENUM_PREFIXES["status"]).lower() for p in plans]


def beats(a: float, b: float, direction: str, tol: float) -> bool:
    """Whether `a` is better than `b` beyond the noise tolerance."""
    return a > b * (1 + tol) if direction == "max" else a < b * (1 - tol)


def no_worse(a: float, b: float, direction: str, tol: float) -> bool:
    """Whether `a` is at least as good as `b` within the noise tolerance."""
    return a >= b * (1 - tol) if direction == "max" else a <= b * (1 + tol)


def frontier(points: list[Point], objs: dict[str, str], tols: dict[str, float]) -> list[Point]:
    """Return the points no other point dominates.

    q dominates p when q is no worse than p on every objective and better beyond that objective's
    noise tolerance on at least one. Only points measured on every objective are comparable.
    """
    comparable = [p for p in points if all(p.mean(n) is not None for n in objs)]

    def dominates(q: Point, p: Point) -> bool:
        return all(no_worse(q.mean(n), p.mean(n), d, tols[n]) for n, d in objs.items()) and any(
            beats(q.mean(n), p.mean(n), d, tols[n]) for n, d in objs.items()
        )

    return [p for p in comparable if not any(dominates(q, p) for q in comparable if q is not p)]


# ---------------------------------------------------------------- drawing


def style(th: Theme) -> None:
    """Apply the theme to matplotlib's rcParams."""
    plt.rcParams.update(
        {
            "figure.facecolor": th["surface"],
            "axes.facecolor": th["surface"],
            "savefig.facecolor": th["surface"],
            "text.color": th["ink"],
            "axes.labelcolor": th["ink2"],
            "xtick.color": th["ink2"],
            "ytick.color": th["ink2"],
            "axes.edgecolor": th["grid"],
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": th["grid"],
            "grid.linewidth": 0.8,
            "axes.axisbelow": True,
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "legend.frameon": False,
        }
    )


def dot(face: str, edge: str) -> Line2D:
    """Legend marker for a scatter point."""
    return Line2D(
        [],
        [],
        marker="o",
        ls="",
        markerfacecolor=face,
        markeredgecolor=edge,
        markeredgewidth=1.8,
        markersize=7,
    )


def save(fig: Figure, out: Path, name: str) -> None:
    """Save and close the figure."""
    fig.tight_layout()
    fig.savefig(out / name, dpi=170)
    plt.close(fig)


def draw_progress(th: Theme, points: list[Point], hl: Headline, out: Path) -> None:
    """Every experiment in run order with the best promoted so far as a line."""
    metric, direction = hl.metric, hl.direction
    seq = sorted((p for p in points if p.mean(metric)), key=lambda p: p.time)
    better = (lambda a, b: a > b) if direction == "max" else (lambda a, b: a < b)
    fig, ax = plt.subplots(figsize=(11.5, 5.8))
    best, line, records = None, [], []
    for i, p in enumerate(seq):
        if p.on_main and (best is None or better(p.mean(metric), best)):
            best = p.mean(metric)
            records.append(i)
        line.append(best if best is not None else float("nan"))
        lo, hi = p.span(metric)
        ax.vlines(i, lo, hi, color=th["blue"] if p.on_main else th["muted"], lw=1.6, alpha=0.3)
    ax.step(range(len(seq)), line, where="post", color=th["blue"], lw=2.4, zorder=3)
    for on_main in (False, True):
        sel = [(i, p) for i, p in enumerate(seq) if p.on_main == on_main]
        ax.scatter(
            [i for i, _ in sel],
            [p.mean(metric) for _, p in sel],
            s=58,
            zorder=4,
            linewidth=1.8,
            facecolor=th["blue"] if on_main else th["surface"],
            edgecolor=th["blue"] if on_main else th["muted"],
        )
    for i in (records[0], records[-1]):
        ax.annotate(
            f"{seq[i].mean(metric):.0f}",
            (i, seq[i].mean(metric)),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color=th["ink2"],
        )
    ax.set_xlabel("Experiment")
    ax.set_ylabel(pretty(metric))
    ax.set_title(
        f"{pretty(metric)}: best promoted {seq[records[0]].mean(metric):.0f} → {line[-1]:.0f}"
    )
    ax.legend(
        [dot(th["blue"], th["blue"]), dot(th["surface"], th["muted"])],
        ["Promoted", "Not promoted"],
        loc="lower right",
    )
    ax.set_xlim(-1, len(seq))
    ax.margins(y=0.12)
    ax.set_ylim(bottom=0)
    save(fig, out, "progress.png")


def draw_frontier(th: Theme, points: list[Point], hl: Headline, out: Path) -> None:
    """Headline versus each latency objective, with the Pareto frontier."""
    metric, objs = hl.metric, hl.objs
    latency = [n for n in objs if n != metric and any(p.mean(n) for p in points)]
    if not latency:
        return
    front = frontier(points, objs, hl.tols)
    rows = -(-len(latency) // 2)
    fig, axes = plt.subplots(rows, 2, figsize=(11.2, 4.6 * rows), squeeze=False, sharey=True)
    for ax, name in zip(axes.flat, latency, strict=False):
        for p in (q for q in points if q.mean(name)):
            ax.scatter(
                [p.mean(name)],
                [p.mean(metric)],
                s=60,
                linewidth=1.8,
                zorder=3,
                facecolor=th["blue"] if p.on_main else th["surface"],
                edgecolor=th["blue"] if p.on_main else th["muted"],
            )
        fr = sorted((p for p in front if p.mean(name)), key=lambda p: p.mean(name))
        ax.plot(
            [p.mean(name) for p in fr],
            [p.mean(metric) for p in fr],
            color=th["orange"],
            lw=2,
            zorder=2,
        )
        ax.scatter(
            [p.mean(name) for p in fr],
            [p.mean(metric) for p in fr],
            s=120,
            facecolor="none",
            edgecolor=th["orange"],
            linewidth=2,
            zorder=4,
        )
        ax.set_xlabel(pretty(name))
        ax.set_ylim(0, max(p.mean(metric) for p in points if p.mean(metric)) * 1.1)
    for ax in axes.flat[len(latency) :]:
        ax.set_visible(False)
    for row in axes:
        row[0].set_ylabel(pretty(metric))
    axes.flat[0].legend(
        [
            dot(th["blue"], th["blue"]),
            dot(th["surface"], th["muted"]),
            Line2D([], [], color=th["orange"], lw=2),
        ],
        ["Promoted", "Not promoted", "Pareto frontier"],
        loc="lower left",
    )
    save(fig, out, "frontier.png")


def draw_latency(th: Theme, points: list[Point], hl: Headline, out: Path) -> None:
    """Each latency objective along the result branch."""
    metric, objs = hl.metric, hl.objs
    names = [n for n in objs if n != metric]
    if not names:
        return
    main = sorted((p for p in points if p.on_main), key=lambda p: p.order)
    fig, axes = plt.subplots(1, len(names), figsize=(4.4 * len(names), 4.2), squeeze=False)
    for ax, name in zip(axes[0], names, strict=True):
        pts = [p for p in main if p.mean(name)]
        ax.plot(
            range(len(pts)),
            [p.mean(name) for p in pts],
            color=th["blue"],
            lw=2,
            marker="o",
            markersize=7,
            markeredgecolor=th["surface"],
            markeredgewidth=2,
        )
        for i, p in enumerate(pts):
            ax.vlines(i, *p.span(name), color=th["blue"], lw=2, alpha=0.3)
        ax.set_title(pretty(name))
        ax.set_xticks(range(len(pts)))
        ax.set_xticklabels([p.commit[:7] for p in pts], rotation=60, ha="right", fontsize=8)
        ax.set_xlim(-0.4, len(pts) - 0.6)
        ax.margins(y=0.15)
        ax.set_ylim(bottom=0)
    save(fig, out, "latency.png")


def draw_plans(th: Theme, statuses: list[str], out: Path) -> None:
    """Plans by outcome."""
    order = [s for s in STATUSES if s in statuses]
    counts = [statuses.count(s) for s in order]
    fig, ax = plt.subplots(figsize=(7.5, 0.7 * len(order) + 1.6))
    colors = {"won": th["blue"], "rejected": th["orange"]}
    ax.barh(
        [s.capitalize() for s in order][::-1],
        counts[::-1],
        height=0.6,
        color=[colors.get(s, th["muted"]) for s in order][::-1],
    )
    for i, c in enumerate(counts[::-1]):
        ax.text(c + 0.15, i, str(c), va="center", color=th["ink2"])
    ax.set_title("Plans by outcome")
    ax.grid(axis="y", visible=False)
    save(fig, out, "plans.png")


def write_report(out: Path, points: list[Point], metric: str) -> None:
    """Write the promoted heads as a table, and the images."""
    main = sorted((p for p in points if p.on_main and p.mean(metric)), key=lambda p: p.order)
    rows = ["| Head | " + pretty(metric) + " | Change | Label |", "|:--|--:|--:|:--|"]
    for i, p in enumerate(main):
        change = "" if i == 0 else f"{p.mean(metric) / main[i - 1].mean(metric) - 1:+.1%}"
        rows.append(f"| `{p.commit[:7]}` | {p.mean(metric):.1f} | {change} | {p.label} |")
    images = [
        f"![{n}]({n})"
        for n in ("progress.png", "frontier.png", "latency.png", "plans.png")
        if (out / n).exists()
    ]
    (out / "report.md").write_text(
        "\n".join(["# Optimization progress", "", *rows, "", *images, ""])
    )


def build(run: CliRun, promoted: list[str], out: Path, theme: str = "light") -> None:
    """Write the charts and report from vibesys-cli's reports; `promoted` is the branch history."""
    points, hl = load_points(run)
    if hl is None:
        msg = "vibesys-cli reports no measured commit for this task: nothing to plot"
        raise SystemExit(msg)
    mark_main(points, promoted)
    th = THEMES[theme]
    style(th)
    out.mkdir(parents=True, exist_ok=True)
    draw_progress(th, points, hl, out)
    draw_frontier(th, points, hl, out)
    draw_latency(th, points, hl, out)
    draw_plans(th, read_plan_statuses(run), out)
    write_report(out, points, hl.metric)


def cli_runner(cli: str) -> CliRun:
    """Run the vibesys-cli at `cli`; a failed call stops the script with the CLI's own message."""

    def run(args: Sequence[str]) -> str:
        argv = [cli, *args]
        # lint-waiver: LW-715302 [S603]; a fixed argv with no shell; calling vibesys-cli is the point.
        done = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        if done.returncode:
            msg = f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip() or done.stdout.strip()}"
            raise SystemExit(msg)
        return done.stdout

    return run


def main() -> None:
    """Parse the command line and build the charts."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--branch", required=True, help="result branch, vibesys/<task>")
    ap.add_argument("--repo", type=Path, default=Path())
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--theme", choices=THEMES, default="light")
    ap.add_argument(
        "--cli",
        default=os.environ.get("VIBESYS_CLI", "vibesys-cli"),
        help="the vibesys-cli to run; $VIBESYS_CLI by default",
    )
    a = ap.parse_args()
    build(cli_runner(a.cli), branch_commits(a.repo, a.branch), a.out, a.theme)


if __name__ == "__main__":
    main()
