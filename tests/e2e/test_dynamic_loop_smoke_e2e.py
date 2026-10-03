"""Smoke tier: the real dynamic loop, process boundary, and agents on a Fake cluster.

Skipped unless ``VIBESYS_E2E_AGENTS=1`` and the provider CLI is on PATH. Run it
through ``scripts/smoke_dynamic_loop.sh`` before every live hardware run.

Each scenario launches the installed ``vibesys`` CLI as an operator does
(launcher, then engine) with ``--outer-loop dynamic --headless`` against the
Slurm run environment. The cluster is ``vs_slurm.fake_connector`` in executing
mode: every production job script runs on this host. The GPU node's profiler
capture is answered by the Fake remote interpreter (one trace and a summary),
so the profile path runs end to end without a GPU. Agents are the real CLIs
(Claude Haiku by default; ``VIBESYS_SMOKE_PROVIDER=codex`` selects Codex). The
input is ``dynamic_smoke/bundle``: a slow prime counter whose accuracy check
and benchmark finish in seconds.

The assertions are loop invariants read from the run's own records
(``tests.support.loop_invariants``), never agent choices. Every run prints a
one-line summary of wall time, tokens, and cost.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.loop_invariants import (
    RunRecords,
    Violation,
    check,
    prompt_paths_of,
    summarize,
)

from vs_slurm.fake_connector import executing_cluster

if TYPE_CHECKING:
    from collections.abc import Callable

    type Events = list[dict[str, object]]

ENABLE_ENV = "VIBESYS_E2E_AGENTS"
PROVIDER_ENV = "VIBESYS_SMOKE_PROVIDER"
_BUNDLE = Path(__file__).parent / "dynamic_smoke" / "bundle"
#: Per provider: the CLI binary and the agent.toml body.
_PROVIDERS = {
    "claude": (
        "claude",
        '[model]\nname = "claude-haiku-4-5-20251001"\n[agent]\ncli_provider = "claude"\n',
    ),
    "codex": (
        "codex",
        '[model]\nname = "gpt-6-luna"\n[thinking]\nlevel = "low"\n[agent]\ncli_provider = "codex"\n',
    ),
}
#: A deadlock guard for one run; a healthy run ends in a few minutes.
_RUN_DEADLINE_S = 1200.0
#: The run-host stop grace (vibesys.run.host STOP_GRACE_S) plus teardown.
_STOP_BOUND_S = 90.0
_POLL_S = 0.5

# The GPU node's profiler, faked like the cluster: the remote interpreter runs
# every command with this host's Python, except rocprof's trusted capture,
# which it answers as ``remote_capture.py --print-output`` does: one trace
# directory under the requested profile store and the capture summary.
_REMOTE_PYTHON = """\
#!/bin/sh
if [ "$1" = "rocprof_profiler/remote_capture.py" ]; then
  while [ "$#" -gt 0 ] && [ "$1" != "--profiles" ]; do shift; done
  mkdir -p "$2/timeline-1"
  printf 'function,share\\ncount_primes,0.97\\n' > "$2/timeline-1/stats.csv"
  printf 'Timeline: count_primes (primes.py) holds 97%% of CPU time.\\n'
  exit 0
fi
exec {python} "$@"
"""

pytestmark = pytest.mark.e2e


def _provider() -> str:
    return os.environ.get(PROVIDER_ENV, "claude")


def _requires_cli() -> pytest.MarkDecorator:
    enabled = os.environ.get(ENABLE_ENV) == "1"
    binary = _PROVIDERS[_provider()][0]
    reason = f"set {ENABLE_ENV}=1" if not enabled else f"{binary} is not on PATH"
    return pytest.mark.skipif(not enabled or shutil.which(binary) is None, reason=reason)


@dataclass
class SmokeRun:
    """One operator-style launch over a fresh project, state home, and Fake cluster."""

    base: Path
    project: Path = field(init=False)
    cluster: Path = field(init=False)
    state_home: Path = field(init=False)
    # Prompt path -> whether it existed when its turn's start was observed.
    observed: dict[Path, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project = self.base / "project"
        self.state_home = self.base / "state-home"
        shutil.copytree(_BUNDLE, self.project, ignore=shutil.ignore_patterns("__pycache__"))
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.name=smoke",
                "-c",
                "user.email=smoke@invalid",
                "commit",
                "-qm",
                "input",
            ],
        ):
            run_test_command([*argv], cwd=self.project, check=True, text=True)
        self.cluster = executing_cluster(self.base / "cluster")
        remote = self.base / "remote"
        remote.mkdir()
        remote_python = self.base / "remote-python"
        remote_python.write_text(_REMOTE_PYTHON.format(python=sys.executable), encoding="utf-8")
        remote_python.chmod(0o755)
        connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(self.cluster)])
        service = json.dumps([str(remote_python), "service.py", "VIBESYS_DYNAMIC_PORT"])
        (self.base / "slurm.toml").write_text(
            "[slurm]\n"
            'name = "fake"\n'
            f'remote_workspace_root = "{remote}"\n'
            "poll_interval_seconds = 1.0\n"
            f'transport = {{ kind = "connector", command = {connector} }}\n'
            "[vibesys]\n"
            f'remote_python = "{remote_python}"\n'
            "[vibesys.service]\n"
            f"command = {service}\n"
            'readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/"\n'
            "startup_timeout_seconds = 30\n",
            encoding="utf-8",
        )
        (self.base / "agent.toml").write_text(_PROVIDERS[_provider()][1], encoding="utf-8")

    def launch(self) -> subprocess.Popen[bytes]:
        """Start ``vibesys`` as an operator does, in its own process group."""
        executable = shutil.which("vibesys") or str(Path(sys.executable).parent / "vibesys")
        argv = [
            executable,
            "--outer-loop", "dynamic", "--headless",
            "--project", str(self.project),
            "--config", str(self.base / "agent.toml"),
            "--run-environment", "slurm",
            "--slurm-config", str(self.base / "slurm.toml"),
            "--profiler", "rocprof", "--backend", "rocm",
            "--max-rounds", "1", "--max-in-flight", "2",
        ]  # fmt: skip
        environment: dict[str, str] = {**os.environ, "VIBESYS_STATE_HOME": str(self.state_home)}
        environment.pop("CLAUDECODE", None)
        log = (self.base / "vibesys.log").open("wb")
        # lint-waiver: LW-731201 [S603]; the smoke tier must cross the real
        # > process boundary (launcher, engine, signals) that in-process runs skip;
        # > the argv is fixed above.
        return subprocess.Popen(  # noqa: S603
            argv,
            cwd=self.base,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def logs_dir(self) -> Path | None:
        """Return the run's logs directory once the engine has created it."""
        found = sorted(self.state_home.glob("projects/*/runs/*/logs/core-events.jsonl"))
        return found[-1].parent if found else None

    def events(self) -> list[dict[str, object]]:
        """Return the core events journaled so far."""
        logs = self.logs_dir()
        if logs is None:
            return []
        lines = (logs / "core-events.jsonl").read_text(encoding="utf-8").splitlines()
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break  # a line still being written
        return events

    def watch(
        self, process: subprocess.Popen[bytes], until: Callable[[Events], bool] | None = None
    ) -> None:
        """Observe turn starts until ``process`` exits or ``until(events)`` holds.

        At each newly journaled turn start, record whether every run path its
        prompt names exists: worktrees are removed by the time the run ends.
        """
        seen = 0
        deadline = time.monotonic() + _RUN_DEADLINE_S
        while process.poll() is None and time.monotonic() < deadline:
            events = self.events()
            for event in events[seen:]:
                if event.get("type") == "agent_execution_started":
                    for path in prompt_paths_of(event, self.roots()):
                        self.observed.setdefault(path, self._present(path))
            seen = len(events)
            if until is not None and until(events):
                return
            time.sleep(_POLL_S)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            pytest.fail(f"run exceeded {_RUN_DEADLINE_S} s; see {self.base / 'vibesys.log'}")

    def _present(self, path: Path) -> bool:
        """Return whether ``path`` exists, a relative one in some agent workspace."""
        if path.is_absolute():
            return path.exists()
        workspaces = [
            self.project,
            *self.project.glob(".vibesys/state/local/runs/*/worktrees/*/workspace"),
        ]
        return any((workspace / path).exists() for workspace in workspaces)

    def roots(self) -> tuple[Path, ...]:
        """Return the run-owned directories prompt paths may point into."""
        return (self.project, self.state_home, self.base / "remote")

    def records(self) -> RunRecords:
        """Load the finished run's records."""
        logs = self.logs_dir()
        assert logs is not None, (self.base / "vibesys.log").read_text(encoding="utf-8")[-4000:]
        states = sorted(self.project.glob(".vibesys/state/runs/*/dynamic/state.json"))
        return RunRecords.load(logs, states[-1] if states else None, self.cluster)

    def verdict(self, *, stop_grace_s: float | None = None) -> list[Violation]:
        """Check the invariants and print the run's one-line summary."""
        records = self.records()
        violations = check(
            records,
            roots=self.roots(),
            exists=lambda path: self.observed.get(path, self._present(path)),
            stop_grace_s=stop_grace_s,
        )
        summary = summarize(records).line()
        line = (
            f"SMOKE {self.base.name} provider={_provider()} {summary} violations={len(violations)}"
        )
        print(line)  # noqa: T201  # lint-waiver: LW-731202 [T201]; the smoke tier's contract is one printed summary line per run that the script greps; logging would need handler setup in a test.
        with (self.base.parent / "smoke-summary.txt").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.writelines(f"  {v.invariant.value}: {v.detail}\n" for v in violations)
        return violations


@_requires_cli()
def test_a_dynamic_run_keeps_the_loop_invariants(tmp_path: Path) -> None:
    smoke = SmokeRun(tmp_path)
    process = smoke.launch()
    smoke.watch(process)

    assert smoke.verdict() == []


def _evaluation_submitted(events: list[dict[str, object]]) -> bool:
    return any(
        event.get("type") == "async_operation_lifecycle"
        and isinstance(event.get("data"), dict)
        and event["data"].get("operation_kind") == "evaluation"  # ty: ignore[unresolved-attribute]
        and event["data"].get("state") == "submitted"  # ty: ignore[unresolved-attribute]
        for event in events
    )


@_requires_cli()
def test_ctrl_c_mid_run_stops_within_the_grace_and_leaves_no_job(tmp_path: Path) -> None:
    smoke = SmokeRun(tmp_path)
    process = smoke.launch()
    smoke.watch(process, until=_evaluation_submitted)
    assert process.poll() is None, "the run ended before any agent submitted an evaluation"

    # A terminal's Ctrl-C reaches the whole foreground process group.
    os.killpg(process.pid, signal.SIGINT)
    smoke.watch(process)

    assert smoke.verdict(stop_grace_s=_STOP_BOUND_S) == []
