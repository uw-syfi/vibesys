# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.8"]
# ///
"""Plot an optimization's progress from its state directory.

Reads `<state>/measurements/*.jsonl` (written by measure's measurements.py), `<state>/plans/*.md`,
the task's `objectives.toml` and the result branch, and writes to `--out`:

  progress.png  every experiment in run order, with the best so far as a line
  frontier.png  headline versus each latency objective, with the Pareto frontier
  latency.png   each latency objective along the result branch
  plans.png     plans by outcome
  report.md     the promoted heads as a table, and the images

The inputs must follow the formats in `vibesys:state` and `vibesys:measure`; nothing is repaired.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib as mpl
import tomllib

mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

if TYPE_CHECKING:
    from matplotlib.figure import Figure

Theme = dict[str, str]
THEMES: dict[str, Theme] = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#8a8984",
              "grid": "#e6e5e1", "blue": "#2a78d6", "orange": "#eb6834"},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#8a8984",
             "grid": "#2e2e2c", "blue": "#3987e5", "orange": "#d95926"},
}
STATUSES = ["won", "returned", "chosen", "proposed", "parked", "rejected"]


@dataclass
class Point:
    """One commit's pooled measurements."""

    commit: str
    time: str
    label: str
    values: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    on_main: bool = False
    order: int = -1

    def mean(self, name: str) -> float | None:
        """Mean of the pooled samples for `name`, or None if unmeasured."""
        v = self.values.get(name)
        return sum(v) / len(v) if v else None

    def span(self, name: str) -> tuple[float, float]:
        """Min and max of the pooled samples for `name`."""
        return min(self.values[name]), max(self.values[name])


@dataclass
class Headline:
    """The headline metric, its direction, every objective's direction and the noise tolerance."""

    metric: str
    direction: str
    objs: dict[str, str]
    tol: float


def pretty(name: str) -> str:
    """Axis label for a metric name."""
    return name.replace("_", " ").capitalize()


def load_points(state: Path) -> tuple[list[Point], str, str]:
    """Pool every passing record's samples into one point per commit.

    Returns the points by time, the headline metric and its direction.
    """
    points: dict[str, Point] = {}
    metric = direction = ""
    for f in sorted((state / "measurements").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r["check"] != "pass":
                continue
            metric, direction = r["metric"], r["direction"]
            p = points.setdefault(r["commit"], Point(r["commit"], r["recorded_at"], r["label"]))
            p.time = min(p.time, r["recorded_at"])
            p.values[metric] += r["samples"]
            for name, v in r.get("observations", {}).items():
                p.values[name] += v
    return sorted(points.values(), key=lambda p: p.time), metric, direction


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


def read_objectives(task_dir: Path) -> tuple[dict[str, str], float]:
    """Objective directions by name and the Pareto relative-noise tolerance."""
    cfg = tomllib.loads((task_dir / "objectives.toml").read_text())
    return {o["name"]: o["direction"] for o in cfg["objective"]}, cfg["pareto"]["relative_noise"]


def read_plan_statuses(state: Path) -> list[str]:
    """The `status:` of every plan file."""
    return [re.search(r"^status:\s*(\S+)", f.read_text(), re.MULTILINE).group(1)
            for f in sorted((state / "plans").glob("plan-*.md"))]


def beats(a: float, b: float, direction: str, tol: float) -> bool:
    """Whether `a` is better than `b` beyond the noise tolerance."""
    return a > b * (1 + tol) if direction == "max" else a < b * (1 - tol)


def no_worse(a: float, b: float, direction: str, tol: float) -> bool:
    """Whether `a` is at least as good as `b` within the noise tolerance."""
    return a >= b * (1 - tol) if direction == "max" else a <= b * (1 + tol)


def frontier(points: list[Point], objs: dict[str, str], tol: float) -> list[Point]:
    """Return the points no other point dominates.

    q dominates p when q is no worse than p on every objective and better beyond the noise
    tolerance on at least one. Only points measured on every objective are comparable.
    """
    comparable = [p for p in points if all(p.mean(n) is not None for n in objs)]

    def dominates(q: Point, p: Point) -> bool:
        return (all(no_worse(q.mean(n), p.mean(n), d, tol) for n, d in objs.items())
                and any(beats(q.mean(n), p.mean(n), d, tol) for n, d in objs.items()))

    return [p for p in comparable if not any(dominates(q, p) for q in comparable if q is not p)]


# ---------------------------------------------------------------- drawing

def style(th: Theme) -> None:
    """Apply the theme to matplotlib's rcParams."""
    plt.rcParams.update({
        "figure.facecolor": th["surface"], "axes.facecolor": th["surface"], "savefig.facecolor": th["surface"],
        "text.color": th["ink"], "axes.labelcolor": th["ink2"], "xtick.color": th["ink2"],
        "ytick.color": th["ink2"], "axes.edgecolor": th["grid"], "axes.spines.top": False,
        "axes.spines.right": False, "axes.grid": True, "grid.color": th["grid"], "grid.linewidth": 0.8,
        "axes.axisbelow": True, "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
        "axes.titlelocation": "left", "legend.frameon": False,
    })


def dot(face: str, edge: str) -> Line2D:
    """Legend marker for a scatter point."""
    return Line2D([], [], marker="o", ls="", markerfacecolor=face, markeredgecolor=edge,
                  markeredgewidth=1.8, markersize=7)


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
        ax.scatter([i for i, _ in sel], [p.mean(metric) for _, p in sel], s=58, zorder=4, linewidth=1.8,
                   facecolor=th["blue"] if on_main else th["surface"],
                   edgecolor=th["blue"] if on_main else th["muted"])
    for i in (records[0], records[-1]):
        ax.annotate(f"{seq[i].mean(metric):.0f}", (i, seq[i].mean(metric)), xytext=(0, 10),
                    textcoords="offset points", ha="center", fontsize=9, color=th["ink2"])
    ax.set_xlabel("Experiment")
    ax.set_ylabel(pretty(metric))
    ax.set_title(f"{pretty(metric)}: best promoted {seq[records[0]].mean(metric):.0f} → {line[-1]:.0f}")
    ax.legend([dot(th["blue"], th["blue"]), dot(th["surface"], th["muted"])], ["Promoted", "Not promoted"],
              loc="lower right")
    ax.set_xlim(-1, len(seq))
    ax.margins(y=0.12)
    ax.set_ylim(bottom=0)
    save(fig, out, "progress.png")


def draw_frontier(th: Theme, points: list[Point], hl: Headline, out: Path) -> None:
    """Headline versus each latency objective, with the Pareto frontier."""
    metric, objs = hl.metric, hl.objs
    latency = [n for n in objs if n != metric and any(p.mean(n) for p in points)]
    front = frontier(points, objs, hl.tol)
    rows = -(-len(latency) // 2)
    fig, axes = plt.subplots(rows, 2, figsize=(11.2, 4.6 * rows), squeeze=False, sharey=True)
    for ax, name in zip(axes.flat, latency, strict=False):
        for p in (q for q in points if q.mean(name)):
            ax.scatter([p.mean(name)], [p.mean(metric)], s=60, linewidth=1.8, zorder=3,
                       facecolor=th["blue"] if p.on_main else th["surface"],
                       edgecolor=th["blue"] if p.on_main else th["muted"])
        fr = sorted((p for p in front if p.mean(name)), key=lambda p: p.mean(name))
        ax.plot([p.mean(name) for p in fr], [p.mean(metric) for p in fr], color=th["orange"], lw=2, zorder=2)
        ax.scatter([p.mean(name) for p in fr], [p.mean(metric) for p in fr], s=120, facecolor="none",
                   edgecolor=th["orange"], linewidth=2, zorder=4)
        ax.set_xlabel(pretty(name))
        ax.set_ylim(0, max(p.mean(metric) for p in points if p.mean(metric)) * 1.1)
    for ax in axes.flat[len(latency):]:
        ax.set_visible(False)
    for row in axes:
        row[0].set_ylabel(pretty(metric))
    axes.flat[0].legend([dot(th["blue"], th["blue"]), dot(th["surface"], th["muted"]),
                         Line2D([], [], color=th["orange"], lw=2)],
                        ["Promoted", "Not promoted", "Pareto frontier"], loc="lower left")
    save(fig, out, "frontier.png")


def draw_latency(th: Theme, points: list[Point], hl: Headline, out: Path) -> None:
    """Each latency objective along the result branch."""
    metric, objs = hl.metric, hl.objs
    names = [n for n in objs if n != metric]
    main = sorted((p for p in points if p.on_main), key=lambda p: p.order)
    fig, axes = plt.subplots(1, len(names), figsize=(4.4 * len(names), 4.2), squeeze=False)
    for ax, name in zip(axes[0], names, strict=True):
        pts = [p for p in main if p.mean(name)]
        ax.plot(range(len(pts)), [p.mean(name) for p in pts], color=th["blue"], lw=2, marker="o", markersize=7,
                markeredgecolor=th["surface"], markeredgewidth=2)
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
    ax.barh([s.capitalize() for s in order][::-1], counts[::-1], height=0.6,
            color=[colors.get(s, th["muted"]) for s in order][::-1])
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
    images = [f"![{n}]({n})" for n in ("progress.png", "frontier.png", "latency.png", "plans.png")]
    (out / "report.md").write_text("\n".join(["# Optimization progress", "", *rows, "", *images, ""]))


def build(state: Path, task_dir: Path, promoted: list[str], out: Path, theme: str = "light") -> None:
    """Write the charts and report for a state directory; `promoted` is the branch history."""
    points, metric, direction = load_points(state)
    mark_main(points, promoted)
    objs, tol = read_objectives(task_dir)
    th = THEMES[theme]
    style(th)
    out.mkdir(parents=True, exist_ok=True)
    hl = Headline(metric, direction, objs, tol)
    draw_progress(th, points, hl, out)
    draw_frontier(th, points, hl, out)
    draw_latency(th, points, hl, out)
    draw_plans(th, read_plan_statuses(state), out)
    write_report(out, points, metric)


def main() -> None:
    """Parse the command line and build the charts."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True, type=Path, help=".vibesys-state/optimize/<task>")
    ap.add_argument("--task-dir", required=True, type=Path, help=".vibesys/tasks/<task>")
    ap.add_argument("--branch", required=True, help="result branch, vibesys/<task>")
    ap.add_argument("--repo", type=Path, default=Path())
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--theme", choices=THEMES, default="light")
    a = ap.parse_args()
    build(a.state, a.task_dir, branch_commits(a.repo, a.branch), a.out, a.theme)


if __name__ == "__main__":
    main()
