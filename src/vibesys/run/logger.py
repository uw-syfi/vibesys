"""Run logging: the per-run log file, ``lprint``, and the stderr tee."""

import re
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO, cast

from vibesys.agent_runner import log_and_print

# Matches ANSI escape sequences (CSI colors, cursor control, and simple
# single-character escapes) so the durable log stays plain text even when
# subprocesses write colored diagnostics to stderr.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return _ANSI_ESCAPE_RE.sub("", text)


class _TeeWriter:
    """Pass writes through to the real stream and mirror them, ANSI-free, to the log."""

    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, text: str) -> int:
        self._primary.write(text)
        self._secondary.write(strip_ansi(text))
        return len(text)

    def flush(self):
        self._primary.flush()
        self._secondary.flush()

    def isatty(self):
        return False


class _CurrentLogWriter:
    """Stable writer that follows a :class:`RunLogger` across file switches."""

    def __init__(
        self,
        write: Callable[[str], int],
        flush: Callable[[], None],
        is_closed: Callable[[], bool],
    ) -> None:
        self._write = write
        self._flush = flush
        self._is_closed = is_closed

    def write(self, text: str) -> int:
        return self._write(text)

    def flush(self) -> None:
        self._flush()

    def isatty(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return self._is_closed()


class RunLogger:
    """Owns the current run log file and the process stderr tee.

    Components that need to log for the lifetime of a run hold
    ``logger.lprint``; it always writes to the *current* log file, so
    log-file switches (``switch``) retarget every holder at once.

    The stderr tee is unconditional: it captures diagnostics (writes still
    reach the real stderr untouched), so it is safe regardless of which
    renderer owns the terminal. The log copy has ANSI escapes stripped so
    ``run-*.log`` stays plain text.

    ``tee_stderr=False`` builds a logger that owns only its file and never
    touches the process-global ``sys.stderr``. This is required for concurrent
    per-candidate sub-loggers: only the one top-level run logger may own the
    stderr tee, or overlapping loggers would fight over (and mis-restore) the
    global handle. A no-tee logger's ``switch``/``close`` leave stderr alone.
    """

    def __init__(self, log_dir: Path, *, tee_stderr: bool = True) -> None:
        self.log_dir = log_dir
        self._tee_stderr = tee_stderr
        self._lock = threading.RLock()
        run_started = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"run-{run_started}.log"
        self.file = self.path.open("a", encoding="utf-8")
        self.writer = cast(
            TextIO,
            _CurrentLogWriter(self._write_current, self._flush_current, self._current_closed),
        )
        self._original_stderr = sys.stderr
        self._stderr_tee: _TeeWriter | None = None
        if tee_stderr:
            self._stderr_tee = _TeeWriter(self._original_stderr, self.writer)
            sys.stderr = self._stderr_tee

    def _write_current(self, text: str) -> int:
        with self._lock:
            return self.file.write(text)

    def _flush_current(self) -> None:
        with self._lock:
            self.file.flush()

    def _current_closed(self) -> bool:
        with self._lock:
            return self.file.closed

    def lprint(self, text: str) -> None:
        log_and_print(text, self.writer)

    def switch(self, label: int | str):
        """Switch to a per-phase log file (``run-<datetime>-<label>.log``).

        *label* is stringified into the file name.  Integer labels get a
        ``step`` prefix for backward compatibility with the curriculum
        loop's step-number usage (e.g. ``switch(3)`` → ``run-<ts>-step3.log``).
        Callers that want a different prefix (e.g. ``round007``) should pass
        a string.

        The previous log file is flushed and closed after the new file becomes
        ``file``. The stderr tee is updated to write to the new file as well.
        Returns the new file handle.
        """
        with self._lock:
            previous_file = self.file
            previous_file.flush()
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            suffix = f"step{label}" if isinstance(label, int) else label
            new_path = self.log_dir / f"run-{ts}-{suffix}.log"
            new_file = new_path.open("a", encoding="utf-8")
            self.path = new_path
            self.file = new_file
            previous_file.close()
            return new_file

    def close(self) -> None:
        with self._lock:
            if self._stderr_tee is not None and sys.stderr is self._stderr_tee:
                sys.stderr = self._original_stderr
            self.file.close()
