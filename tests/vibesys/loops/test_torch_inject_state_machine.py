"""Property tests for the injection's capture state machine, run in-process.

``test_torch_inject.py`` covers startup, signal delivery, and exit in real
subprocesses. This file covers only ``_Capture``'s own logic (queued starts,
repeated windows, per-window acknowledgements) against a reference model,
over arbitrary interleavings of the three events a controller and the
readiness watcher can produce. Signal handlers are replaced by direct calls,
which is exactly what the handlers do.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.loops.torch_inject_fixtures import INJECT_DIR

_OPS = ("ready", "start", "stop")


def _fake_torch() -> tuple[types.ModuleType, types.ModuleType, list[str]]:
    """Minimal torch/torch.profiler; returns (torch, torch.profiler, active-session log)."""
    active: list[str] = []
    profiler = types.ModuleType("torch.profiler")

    class ProfilerActivity:
        CPU = "cpu"
        CUDA = "cuda"

    class _Profile:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            assert not active, "two torch.profiler sessions open at once"
            active.append("session")

        def stop(self) -> None:
            active.pop()

        def export_chrome_trace(self, path: str) -> None:
            Path(path).write_text(json.dumps({"traceEvents": [{"ph": "X"}]}))

    vars(profiler).update(ProfilerActivity=ProfilerActivity, profile=_Profile)
    torch = types.ModuleType("torch")
    vars(torch).update(profiler=profiler, cuda=types.SimpleNamespace(synchronize=lambda: None))
    return torch, profiler, active


@pytest.fixture(scope="module")
def inject_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "vibesys_torch_inject_under_test", INJECT_DIR / "sitecustomize.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("VIBESYS_TORCH_PROFILE", raising=False)  # import-time no-op
        spec.loader.exec_module(module)
    return module


@dataclass
class _Model:
    ready: bool = False
    pending: bool = False
    running: bool = False
    windows: int = 0  # windows started so far
    exported: int = 0


class _Harness:
    """Applies one op to the real ``_Capture`` and to the reference model."""

    def __init__(self, inject_module: types.ModuleType, root: Path) -> None:
        self.root = root
        self.control = root / "control"
        self.capture = inject_module._Capture(  # noqa: SLF001  # LW-920405; the standalone injected script's state machine class is private by design
            out_dir=root / "default", record_shapes=False, control_dir=self.control
        )
        self.model = _Model()
        self.named = 0  # window dirs named via next_window so far

    def _start(self) -> None:
        self.capture.start()
        if not self.model.ready:
            self.model.pending = True
        elif not self.model.running:
            self.model.running = True
            self.model.windows += 1

    def ready(self) -> None:
        if self.model.ready:
            return  # the watcher marks ready exactly once
        fire = self.capture.mark_ready()
        assert fire == self.model.pending
        self.model.ready, self.model.pending = True, False
        if fire:
            self._start()  # the watcher re-delivers the queued SIGUSR1

    def start(self) -> None:
        self.named += 1
        self.control.mkdir(parents=True, exist_ok=True)
        (self.control / "next_window").write_text(str(self.root / f"w{self.named}"))
        self._start()

    def stop(self) -> None:
        self.capture.stop_and_export()
        self.model.pending = False
        if self.model.running:
            self.model.running = False
            self.model.exported += 1


@settings(max_examples=200, deadline=None)
@given(ops=st.lists(st.sampled_from(_OPS), max_size=25))
def test_capture_matches_reference_model(inject_module: types.ModuleType, ops: list[str]) -> None:
    """Any interleaving of ready/start/stop behaves like the reference model.

    Properties: a start before ready is queued (never dropped, never run
    early) and fires exactly once when ready arrives; a stop cancels a
    queued start; at most one profiler session is ever open; every started
    window is acknowledged ``window.started`` and, once stopped,
    ``window.exported`` with exactly one trace in its own directory; nothing
    is ever acknowledged ``window.failed``.
    """
    torch, profiler, active = _fake_torch()
    with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
        mp.setitem(sys.modules, "torch", torch)
        mp.setitem(sys.modules, "torch.profiler", profiler)
        harness = _Harness(inject_module, Path(tmp))
        for op in ops:
            getattr(harness, op)()
            running = harness.model.running
            assert len(active) == (1 if running else 0)
            phase = harness.capture._phase  # noqa: SLF001  # LW-920406; the reference model must check the private capture phase directly
            assert phase == ("running" if running else "idle")

        root, model = harness.root, harness.model
        assert len(list(root.glob("w*/window.started"))) == model.windows
        exported = list(root.glob("w*/window.exported"))
        assert len(exported) == model.exported
        assert not list(root.glob("w*/window.failed"))
        for ack in exported:
            assert len(list(ack.parent.glob("*.pt.trace.json.gz"))) == 1
        assert not (root / "default").exists(), "a named window fell back to the default dir"
