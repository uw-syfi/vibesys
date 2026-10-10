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


def test_the_stack_dump_fires_before_the_timeout() -> None:
    # The timeout failure carries no all-thread stacks; faulthandler's earlier dump to
    # stderr shows where the test was stuck.
    assert 0 < PYTEST["faulthandler_timeout"] < PYTEST["timeout"]


def test_the_timeout_plugin_is_a_dev_dependency() -> None:
    dev = PYPROJECT["dependency-groups"]["dev"]
    assert "pytest-timeout" in dev
