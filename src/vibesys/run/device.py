"""Compute-device coordination for one experiment run.

``DeviceLease`` is a thin coordinator around whichever device the compute
backend selected: env pinning for host-run agents, mid-run reselection,
the background contention monitor, and end-of-run metadata finalization.
It legitimately holds refs to the backend impl and (once the session is
open) the run-environment view; selection logic itself stays in the
backend.
"""

import json
from datetime import datetime
from pathlib import Path


class DeviceLease:
    def __init__(self, backend, *, log_dir: Path, run_environment_view=None) -> None:
        self._backend = backend
        self._log_dir = log_dir
        self._view = run_environment_view
        self.monitor = None

    @property
    def selected_device(self):
        return getattr(self._backend, "selected_device", None)

    def start_monitor(self) -> None:
        """Start backend-specific background monitoring (CUDA: nvidia-smi)."""
        self.monitor = self._backend.make_monitor(self._log_dir)
        if self.monitor is not None:
            self.monitor.start()

    def gpu_env(self) -> dict[str, str]:
        """Env vars to inject into the host-running cli agent runner.

        Today this is just the device pin (``CUDA_VISIBLE_DEVICES`` for cuda),
        derived from whichever device the backend selected.  The deepagents
        path ignores this; the cli path layers it onto the spawned subprocess
        env so it sees the same device the sandbox env was built with.
        """
        dev = self.selected_device
        if dev is None:
            return {}
        return {"CUDA_VISIBLE_DEVICES": str(dev.index)}

    def reselect(self) -> None:
        """Delegate mid-run device rebalance to the backend.

        Restarted sandboxes re-run their ``setup_fns`` (e.g. docker symlinks)
        as part of ``start()`` — no replay logic needed here.
        """
        if self._view is not None and not self._view.host_device_reselect:
            return
        self._backend.reselect_device()
        self.monitor = getattr(self._backend, "_monitor", None)

    def _finalize_metadata(self) -> None:
        """Update ``gpu.json`` with contention summary before closing."""
        gpu_json = self._log_dir / "gpu.json"
        if not gpu_json.exists():
            return

        data = json.loads(gpu_json.read_text())

        contention_log = self._log_dir / "gpu_contention.jsonl"
        contention_events = 0
        if contention_log.exists():
            text = contention_log.read_text().strip()
            if text:
                contention_events = len(text.splitlines())

        data["contention_detected"] = contention_events > 0
        data["contention_events"] = contention_events
        data["finished_at"] = datetime.now().isoformat()
        gpu_json.write_text(json.dumps(data, indent=2))

    def close(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self._finalize_metadata()
