"""The Hypothesis profiles registered in the root ``conftest.py``.

The profiles only matter through what they do to a real pytest run, so most of
these tests run pytest in a subprocess on a planted property. A config-value
assertion would have passed while the database was silently inert (a forced
global seed removes the per-test database key, so nothing is saved or replayed).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import settings

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Records every drawn value, and fails once a value exceeds 1000 when PLANT is set.
_PLANTED_PROPERTY = """
import os
from hypothesis import given, strategies as st

@given(st.integers(min_value=0, max_value=10**6))
def test_planted(n):
    with open(os.environ["DRAW_LOG"], "a") as log:
        log.write(f"{n}\\n")
    assert not (os.environ.get("PLANT") and n > 1000)
"""


@pytest.mark.parametrize("name", ["dev", "ci", "explore", "nightly"])
def test_no_profile_uses_a_wall_clock_deadline(name: str) -> None:
    assert settings.get_profile(name).deadline is None


class _Project:
    """A throwaway test directory run under the repository's ``conftest.py``."""

    def __init__(self, root: Path, profile: str) -> None:
        self.root = root
        self.profile = profile
        (root / "test_planted.py").write_text(_PLANTED_PROPERTY)

    def run(self, *, plant: bool, database: str = "db") -> list[int]:
        """Run pytest once; return the values the property drew, in order."""
        log = self.root / "draws.log"
        log.unlink(missing_ok=True)
        env: dict[str, str] = {
            **os.environ,
            "PYTHONPATH": str(_REPO_ROOT),
            "HYPOTHESIS_PROFILE": self.profile,
            "VIBESYS_HYPOTHESIS_DB": str(self.root / database),
            "DRAW_LOG": str(log),
        }
        env.pop("PLANT", None)
        if plant:
            env["PLANT"] = "1"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "conftest",
                "-p",
                "no:randomly",
                "-p",
                "no:cacheprovider",
                "--no-cov",
                "-q",
                "test_planted.py",
            ],
            cwd=self.root,
            env=env,
            capture_output=True,
            check=False,
        )
        return [int(line) for line in log.read_text().split()] if log.exists() else []


@pytest.mark.parametrize("profile", ["ci", "nightly"])
def test_a_failure_is_saved_by_one_run_and_replayed_first_by_the_next(
    tmp_path: Path, profile: str
) -> None:
    project = _Project(tmp_path, profile)

    first = project.run(plant=True)
    replay = project.run(plant=True)

    assert first[-1] > 1000  # the failing example, shrunk or not
    assert replay[0] == 1001  # the shrunk failure, drawn before anything else
    assert len(replay) < len(first)


def test_the_ci_profile_draws_the_same_examples_every_run(tmp_path: Path) -> None:
    project = _Project(tmp_path, "ci")

    first = project.run(plant=False, database="db1")
    second = project.run(plant=False, database="db2")

    assert len(first) > 1
    assert first == second
