"""The suite keeps a hard per-test bound, so a hung test fails one test and not a shard.

The bound is a backstop, not a test condition: no test may rely on it to pass.
This guard only keeps the configuration from being dropped or reordered, since
a missing bound is invisible until a test hangs. The deterministic bound stays
under half the shard job's cancel time, so the hang is named first.
"""

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PYTEST = PYPROJECT["tool"]["pytest"]["ini_options"]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
CONFTEST = ROOT / "conftest.py"


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


def _job_timeout_minutes(name: str) -> int:
    """The ``timeout-minutes`` of a top-level job in the test workflow."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line == f"  {name}:")
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("   ") and line.endswith(":"):
            break
        stripped = line.strip()
        if stripped.startswith("timeout-minutes:"):
            return int(stripped.split(":", 1)[1].strip())
    message = f"{WORKFLOW} job {name!r} has no timeout-minutes"
    raise AssertionError(message)


def _assigned_constant(name: str) -> int:
    """A module-level integer assignment in the root conftest."""
    tree = ast.parse(CONFTEST.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(node.value, ast.Constant)
        ):
            value = node.value.value
            if isinstance(value, int):
                return value
    message = f"{CONFTEST} does not assign an integer to {name}"
    raise AssertionError(message)


def test_the_deterministic_timeout_stays_under_half_the_shard_budget() -> None:
    # The `test` job is the deterministic shard. Half the cancel time is the
    # room a hang still has after setup and the rest of the shard.
    shard_s = _job_timeout_minutes("test") * 60
    timeout_s = PYTEST["timeout"]
    assert isinstance(timeout_s, int)
    assert timeout_s * 2 < shard_s


def test_real_tiers_and_the_long_running_backstop_keep_their_bounds() -> None:
    # 300 s is the bound real tiers and nightly depth had before the
    # deterministic shard bound was shortened. 3600 s is the e2e, real_contract
    # and slow backstop. Neither moves.
    source = CONFTEST.read_text(encoding="utf-8")
    assert _assigned_constant("_PRESERVED_TIMEOUT_S") == 300
    assert _assigned_constant("_LONG_RUNNING_TIMEOUT_S") == 3600
    assert "pytest.mark.timeout(_PRESERVED_TIMEOUT_S)" in source
    assert "pytest.mark.timeout(_LONG_RUNNING_TIMEOUT_S)" in source
    timeout_s = PYTEST["timeout"]
    assert isinstance(timeout_s, int)
    assert timeout_s < _assigned_constant("_PRESERVED_TIMEOUT_S")
