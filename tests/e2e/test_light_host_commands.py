"""Real processes for the light host commands: what they load, and how long they take.

``tests/architecture/test_light_entrypoints.py`` checks the static import
closure; this runs ``vibesys instances list``, ``vibesys instances stop`` and
``vibesys tasks`` through the real launcher under ``python -X importtime`` and checks the modules
the interpreter actually loaded, deferred imports included. Wall time is
reported against the 300 ms budget (``docs/contributing/desktop-connection.md``)
as a test property and on stdout, never asserted: a test's result must not
depend on timing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest
from tests.support.light_imports import BUDGET_MS, is_heavy, loaded_modules

from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    RecordProperty = Callable[[str, object], None]


def _run_light(
    argv: list[str], env: dict[str, str], record_property: RecordProperty, *, exit_code: int = 0
) -> str:
    started = time.monotonic()
    # lint-waiver: LW-182903 [S603]; the command must be its own interpreter, as the app runs it.
    # > Import cost is per process; an in-process call would reuse modules this test
    # > runner already loaded and measure nothing.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-X", "importtime", "-m", "entrypoints.launcher", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD_S,
        check=False,
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    record_property(f"wall_ms[{' '.join(argv[:2])}]", elapsed_ms)
    sys.stdout.write(f"vibesys {' '.join(argv)}: {elapsed_ms} ms (budget {BUDGET_MS} ms)\n")
    assert result.returncode == exit_code, result.stderr[-2000:]
    loaded = loaded_modules(result.stderr)
    assert "pydantic" in loaded, "importtime reported no modules; the check would be vacuous"
    assert [name for name in loaded if is_heavy(name)] == []
    return result.stdout


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": str(runtime),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }


def test_instances_list_loads_no_engine(
    env: dict[str, str], record_property: RecordProperty
) -> None:
    stdout = _run_light(["instances", "list", "--json"], env, record_property)

    assert json.loads(stdout) == {
        "version": 5,
        "instances": [],
        "ended": [],
        "unreadable": [],
        "unverified": [],
    }


def test_instances_stop_loads_no_engine(
    env: dict[str, str], record_property: RecordProperty
) -> None:
    # The stop requester is imported at module level, so stopping an id that
    # is not live loads everything a stop over the control socket would.
    stdout = _run_light(
        ["instances", "stop", "0123456789ab", "--json"], env, record_property, exit_code=1
    )

    assert json.loads(stdout)["outcome"] == "not_running"


def test_tasks_loads_no_engine(
    tmp_path: Path, env: dict[str, str], record_property: RecordProperty
) -> None:
    task = tmp_path / "project" / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")

    stdout = _run_light(["tasks", str(tmp_path / "project"), "--json"], env, record_property)

    assert [entry["name"] for entry in json.loads(stdout)["tasks"]] == ["spsc"]


def test_example_list_loads_no_engine(env: dict[str, str], record_property: RecordProperty) -> None:
    stdout = _run_light(["example", "list", "--json"], env, record_property)

    names = [entry["name"] for entry in json.loads(stdout)["examples"]]
    assert "data-structures/stack" in names


def test_intent_admission_loads_contracts_without_the_run_engine(
    env: dict[str, str], record_property: RecordProperty
) -> None:
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "entrypoints.host", "post-intent"],
        input=b"",
        env=env,
        capture_output=True,
        timeout=HANG_GUARD_S,
        check=False,
    )
    record_property("admission_cold_ms", round((time.monotonic() - started) * 1000))
    assert result.returncode == 0, result.stderr.decode()[-2000:]
    assert result.stdout
    loaded = loaded_modules(result.stderr.decode())
    assert "vs_proto.api" in loaded
    engines = (
        "vs_runtime._core_loop",
        "vs_runtime.api.infrastructure",
        "vibesys.run",
        "vibesys.api.transport",
        "vs_agent",
        "server",
        "launch",
    )
    assert [
        name
        for name in loaded
        if any(name == root or name.startswith(root + ".") for root in engines)
        # The empty ``server`` package and ``lazy_module``, which imports nothing until a handle is read.
        and name not in {"server", "server.lazy_modules"}
    ] == []
