"""Shared support for the torch.profiler injection test suite.

``resources/profilers/torch/inject/sitecustomize.py`` only cares about a
narrow slice of the real ``torch`` API surface (``torch.cuda.is_available``/
``synchronize``, and ``torch.profiler.profile``/``ProfilerActivity``). This
module writes a tiny fake ``torch`` package that mimics exactly that slice,
so the injection's own logic (arming, delay/duration scheduling, signal
handling, chaining, export-before-sync ordering) can be exercised end to end
via real subprocesses even where real torch is unavailable (this dev
environment has no torch installed at all -- confirmed by
``python -c "import torch"`` failing).

The fake torch is driven entirely by environment variables so each test can
shape its behavior without editing the package:

- ``FAKE_TORCH_GPU``: ``"1"``/``"0"`` -- ``cuda.is_available()`` return value
  (default ``"1"``).
- ``FAKE_TORCH_SYNC_DELAY_S``: seconds ``cuda.synchronize()`` sleeps before
  returning (default ``0``); used to simulate the ROCm post-export hang.
- ``FAKE_TORCH_CALL_LOG``: path to append one ``"<monotonic>\\t<event>"``
  line per call the fake module makes (``torch.imported``,
  ``cuda.is_available``, ``profile.start``, ``profile.stop``,
  ``profile.export_chrome_trace``), used to assert ordering and timing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INJECT_DIR = REPO_ROOT / "resources" / "profilers" / "torch" / "inject"

_FAKE_TORCH_INIT_SOURCE = textwrap.dedent(
    '''
    """Fake torch, mimicking only the API surface sitecustomize.py uses."""

    import os
    import time

    from ._log import log_event as _log_event

    _log_event("torch.imported")


    class cuda:
        @staticmethod
        def is_available() -> bool:
            _log_event("cuda.is_available")
            return os.environ.get("FAKE_TORCH_GPU", "1") == "1"

        @staticmethod
        def synchronize() -> None:
            _log_event("cuda.synchronize.start")
            delay = float(os.environ.get("FAKE_TORCH_SYNC_DELAY_S", "0"))
            if delay:
                time.sleep(delay)
            _log_event("cuda.synchronize.done")


    from . import profiler  # noqa: E402  (torch.profiler must resolve as a submodule)
    '''
)

_FAKE_TORCH_LOG_SOURCE = textwrap.dedent(
    '''
    """Shared call-log helper for the fake torch package."""

    import os
    import time


    def log_event(event: str) -> None:
        path = os.environ.get("FAKE_TORCH_CALL_LOG")
        if not path:
            return
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.monotonic()}\\t{event}\\n")
    '''
)

_FAKE_TORCH_PROFILER_SOURCE = textwrap.dedent(
    '''
    """Fake torch.profiler: only what sitecustomize.py's ``from torch.profiler
    import ProfilerActivity, profile`` needs -- a real importable submodule,
    not just an attribute (matching real torch's package layout).
    """

    import json
    import os
    import time

    from ._log import log_event as _log_event


    class ProfilerActivity:
        CPU = "cpu"
        CUDA = "cuda"


    class profile:
        def __init__(self, activities=None, record_shapes=False, with_stack=False):
            self.activities = activities
            self.record_shapes = record_shapes
            self.with_stack = with_stack
            if os.environ.get("FAKE_TORCH_INIT_FAIL") == "1":
                raise RuntimeError("fake torch: profile() init failure")

        def start(self):
            if os.environ.get("FAKE_TORCH_START_FAIL") == "1":
                raise RuntimeError("fake torch: start() failure")
            _log_event("profile.start")

        def stop(self):
            _log_event("profile.stop")

        def export_chrome_trace(self, path):
            if os.environ.get("FAKE_TORCH_EXPORT_FAIL") == "1":
                raise RuntimeError("fake torch: export_chrome_trace() failure")
            events = [
                {
                    "ph": "X",
                    "name": "fake_kernel",
                    "cat": "kernel",
                    "ts": time.time() * 1e6,
                    "dur": 100,
                    "pid": os.getpid(),
                    "tid": 1,
                    "args": {"correlation": 1},
                }
            ]
            data = {"traceEvents": events, "deviceProperties": []}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            _log_event("profile.export_chrome_trace")
    '''
)


def write_fake_torch(root: Path) -> Path:
    """Write a fake ``torch`` package under *root* and return *root*.

    ``torch.profiler`` is written as a real submodule (``torch/profiler.py``),
    not a nested class, since ``sitecustomize.py`` does
    ``from torch.profiler import ProfilerActivity, profile`` -- an import
    statement that requires an actual importable submodule.

    *root* should be added to ``PYTHONPATH``/``sys.path`` so ``import torch``
    resolves to it.
    """
    package_dir = root / "torch"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text(_FAKE_TORCH_INIT_SOURCE)
    (package_dir / "_log.py").write_text(_FAKE_TORCH_LOG_SOURCE)
    (package_dir / "profiler.py").write_text(_FAKE_TORCH_PROFILER_SOURCE)
    return root


def base_env(
    *,
    out_dir: Path,
    fake_torch_root: Path | None,
    call_log: Path | None = None,
    extra_pythonpath: Path | None = None,
    **overrides: str,
) -> dict[str, str]:
    """Build a subprocess env that arms the real ``inject/sitecustomize.py``.

    ``INJECT_DIR`` always comes first on ``PYTHONPATH`` so it wins the
    ``import sitecustomize`` lookup; *fake_torch_root*/*extra_pythonpath*
    follow it. *overrides* are applied last and win over every default here.
    """
    pythonpath_parts = [str(INJECT_DIR)]
    if fake_torch_root is not None:
        pythonpath_parts.append(str(fake_torch_root))
    if extra_pythonpath is not None:
        pythonpath_parts.append(str(extra_pythonpath))
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(pythonpath_parts),
        "VIBESYS_TORCH_PROFILE_OUT_DIR": str(out_dir),
    }
    if call_log is not None:
        env["FAKE_TORCH_CALL_LOG"] = str(call_log)
    env.update(overrides)
    return env


def run_python(
    script: str, *, env: dict[str, str], cwd: Path | None = None, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    """Run *script* via ``python -c`` under *env*, capturing text output."""
    return subprocess.run(  # noqa: S603  # tracked: #288
        [sys.executable, "-c", script],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_call_log(path: Path) -> list[tuple[float, str]]:
    """Parse a ``FAKE_TORCH_CALL_LOG`` file into ``(monotonic_ts, event)`` pairs."""
    if not path.is_file():
        return []
    events: list[tuple[float, str]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        ts_str, _, name = line.partition("\t")
        events.append((float(ts_str), name))
    return events
