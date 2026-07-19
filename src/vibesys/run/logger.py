"""Run logging: the per-run log file, ``lprint``, and the stderr tee."""

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from vibesys.agent_runner import log_and_print


class _TeeWriter:
    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, text: str) -> int:
        self._primary.write(text)
        self._secondary.write(text)
        return len(text)

    def flush(self):
        self._primary.flush()
        self._secondary.flush()

    def isatty(self):
        return False


class RunLogger:
    """Owns the current run log file and the process stderr redirect.

    Components that need to log for the lifetime of a run hold
    ``logger.lprint``; it always writes to the *current* log file, so
    log-file switches (``switch``) retarget every holder at once.
    """

    def __init__(self, log_dir: Path, *, redirect_stderr: bool) -> None:
        self.log_dir = log_dir
        run_started = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"run-{run_started}.log"
        self.file = self.path.open("a", encoding="utf-8")
        self._original_stderr = sys.stderr
        self.stderr_redirected = redirect_stderr
        if self.stderr_redirected:
            sys.stderr = _TeeWriter(self._original_stderr, self.file)

    def lprint(self, text: str) -> None:
        log_and_print(text, self.file)

    def switch(self, label: int | str):
        """Switch to a per-phase log file (``run-<datetime>-<label>.log``).

        *label* is stringified into the file name.  Integer labels get a
        ``step`` prefix for backward compatibility with the curriculum
        loop's step-number usage (e.g. ``switch(3)`` → ``run-<ts>-step3.log``).
        Callers that want a different prefix (e.g. ``round007``) should pass
        a string.

        The previous log file is flushed but kept open. A new file becomes
        ``file``. When stderr logging is enabled, its tee is updated to
        write to the new file as well.  Returns the new file handle.
        """
        self.file.flush()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"step{label}" if isinstance(label, int) else label
        new_path = self.log_dir / f"run-{ts}-{suffix}.log"
        new_file = new_path.open("a", encoding="utf-8")
        self.path = new_path
        self.file = new_file
        if self.stderr_redirected:
            sys.stderr = _TeeWriter(self._original_stderr, new_file)
        return new_file

    def close(self) -> None:
        if self.stderr_redirected:
            sys.stderr = self._original_stderr
        self.file.close()
