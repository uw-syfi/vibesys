"""The standalone measure scripts against real processes, confined to the real tier."""

from __future__ import annotations

import importlib.util
import io
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_service_owner_stops_and_verifies_its_process_group() -> None:
    module = _load("plugin_service_contract", "skills/search/measure/scripts/service.py")
    assert (
        module.serve(
            [sys.executable, "-c", "import signal; signal.pause()"],
            [sys.executable, "-c", "pass"],
            [sys.executable, "-c", "pass"],
            module.PosixHost(),
        )
        == 0
    )


def test_benchmark_wrapper_holds_the_host_lock_in_its_child() -> None:
    module = _load("benchmark_lock_contract", "skills/search/measure/scripts/measurements.py")
    program = """import fcntl, sys
with open("/tmp/vibesys-benchmark.lock", "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit(1)
"""
    assert (
        module.main(
            ["run", "--", sys.executable, "-c", program],
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            out=io.StringIO(),
            err=io.StringIO(),
            host="host-a",
        )
        == 0
    )
