"""The suite keeps a hard per-test bound, so a hung test fails one test and not a shard.

The bound is a backstop, not a test condition: no test may rely on it to pass.
This guard only keeps the configuration from being dropped or reordered, since
a missing bound is invisible until a test hangs.
"""

import tomllib
from pathlib import Path

PYPROJECT = tomllib.loads(
    (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
)
PYTEST = PYPROJECT["tool"]["pytest"]["ini_options"]


def test_a_hard_timeout_is_configured_with_the_signal_method() -> None:
    assert PYTEST["timeout"] > 0
    # `thread` kills the worker, and CI's `--dist loadgroup` then stalls with no replacement.
    assert PYTEST["timeout_method"] == "signal"


def test_the_stack_dump_comes_from_the_timeout_not_faulthandler_s_watchdog() -> None:
    # faulthandler's watchdog dumps from a thread without the GIL and can segfault the worker
    # it is meant to diagnose; the timeout's own dump runs in Python with the GIL held.
    assert "faulthandler_timeout" not in PYTEST


def test_a_crashed_worker_ends_the_session_instead_of_stalling_it() -> None:
    # `--dist loadgroup` never replaces a dead worker, so the tests it still owed would wait
    # for the CI job timeout; with no restarts allowed xdist stops and reports the crash.
    assert "--max-worker-restart=0" in PYTEST["addopts"].split()


def test_the_timeout_plugin_is_a_dev_dependency() -> None:
    dev = PYPROJECT["dependency-groups"]["dev"]
    assert "pytest-timeout" in dev
