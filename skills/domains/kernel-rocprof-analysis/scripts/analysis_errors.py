"""The one error type every analysis script raises, and a safe JSON reader.

An analysis fails when its input is not a readable profiler report (a missing
directory, a malformed CSV, an empty export). The scripts raise
``AnalysisError`` for every such case, and ``analyze.py`` turns it into a message
on standard error and exit status 1, so a caller tells "no report" from a
report that shows no work.
"""

from __future__ import annotations

import json
from pathlib import Path


class AnalysisError(RuntimeError):
    """A report could not be analyzed; the message says which input and why."""


def analysis_failed(diagnostic: object) -> AnalysisError:
    """Return the error for an unreadable report input or a failed analyzer step."""
    return AnalysisError(str(diagnostic))


def read_json_file(path: Path) -> object:
    """Decode the JSON file at *path*, whatever bytes a workload left in it.

    Python's ``json`` raises ``RecursionError`` past its nesting limit, and invalid
    UTF-8 is a ``UnicodeDecodeError``; both become ``ValueError`` here, so a caller
    handles every undecodable file with one ``except (OSError, ValueError)``.
    """
    data = path.read_bytes()
    try:
        return json.loads(data)
    except RecursionError as exc:
        message = f"{path} nests deeper than the JSON decoder reads"
        raise ValueError(message) from exc
