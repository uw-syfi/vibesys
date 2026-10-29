"""Prove the public fan-out test rejects immediate-return replay.

Run with ``uv run python -m scripts.check_replay_ordering_mutant``. This manual
mutation check builds a disposable source tree; it never patches a running
module or changes the checkout. The normal deterministic suite exercises only
the public API. A successful check requires that same test to reject the mutant
with the specific A-before-B observation, rather than an unrelated exception.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from vs_sim.api import Capture, OsProcesses, ProcessOutcome, ProcessSpec


def main() -> None:
    """Run the public assertion against a disposable immediate-return scheduler."""
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="replay-immediate-") as temporary:
        target = Path(temporary)
        shutil.copytree(root / "libs/vs-core/src/vs_core", target / "vs_core")
        path = target / "vs_core/flows/_replay_run.py"
        source = path.read_text()
        changes = (
            ("        for accepted in self._history:\n            self._release(accepted)\n", ""),
            (
                "        if accepted.sequence <= self._last_sequence or accepted.clock < self._last_clock:",
                "        if False:",
            ),
            (
                "        self._active[call.key] = pending\n",
                "        self._active[call.key] = pending\n"
                "        pending.registered = True\n"
                "        for recorded in self._history:\n"
                "            if isinstance(recorded, AcceptedReplay) and (recorded.key, recorded.identity) == occurrence:\n"
                "                self._release(recorded)\n"
                "                break\n",
            ),
        )
        for before, after in changes:
            if source.count(before) != 1:
                problem = "Scheduler changed: update the explicit immediate-return mutation"
                raise ValueError(problem)
            source = source.replace(before, after)
        path.write_text(source)
        environment = dict(os.environ, PYTHONPATH=str(target) + os.pathsep + str(root))
        processes = OsProcesses()
        result = processes.run(
            ProcessSpec(
                (
                    sys.executable,
                    "-m",
                    "pytest",
                    "libs/vs-core/tests/test_keyed_replay.py::test_fanout_releases_b_then_planning_then_a_with_quiescence",
                    "--no-cov",
                    "-q",
                ),
                cwd=root,
                env=environment,
            ),
            timeout=60,
            output=Capture(1024 * 1024),
        )
        if not isinstance(result, ProcessOutcome):
            problem = "Immediate-return mutation test exceeded its hang guard"
            raise TypeError(problem)
        output = (result.stdout + result.stderr).decode()
        if result.returncode != 1 or "('A', 11" not in output or "('B', 8" not in output:
            problem = f"Expected the ordering assertion to reject the mutant:\n{output}"
            raise RuntimeError(problem)
        sys.stdout.write("Immediate-return replay rejected: A/11 observed where B/8 is required.\n")


if __name__ == "__main__":
    main()
