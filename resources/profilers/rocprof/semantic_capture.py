"""Run the configured ROCprof request as one trusted evaluation stage."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

remote_capture = importlib.import_module("remote_capture")

_MAX_SUMMARY_CHARS = 16_384
_TRUNCATION_MARKER = "\n...[profile summary truncated by VibeSys]"


def _load_result(path: Path) -> tuple[str, list[str]]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    summary = envelope["output"]
    capture_ids = envelope["capture_ids"]
    if envelope.get("missing_fields"):
        message = "configured profile capture lacks required fields: " + ", ".join(
            envelope["missing_fields"]
        )
        raise ValueError(message)
    valid = (
        isinstance(summary, str)
        and isinstance(capture_ids, list)
        and bool(capture_ids)
        and all(isinstance(item, str) and item for item in capture_ids)
    )
    if not valid:
        message = "configured profile capture returned a malformed result"
        raise ValueError(message)
    return summary, capture_ids


def bounded_summary(summary: str) -> str:
    """Keep agent-visible profile output within the semantic evidence contract."""
    if len(summary) <= _MAX_SUMMARY_CHARS:
        return summary
    return summary[: _MAX_SUMMARY_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def run(request_json: str, profiles: Path) -> int:
    """Execute one capture and print the bounded semantic result JSON."""
    with tempfile.TemporaryDirectory(prefix="vibesys-profile-result-") as temporary:
        result_path = Path(temporary) / "result.json"
        code = remote_capture.run_request(None, request_json, result_path, profiles)
        if code != 0:
            return code
        try:
            summary, _capture_ids = _load_result(result_path)
        except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
            sys.stderr.write("configured profile capture returned a malformed result\n")
            return 1
        summary = bounded_summary(summary)
        sys.stdout.write(
            json.dumps(
                {
                    "summary": summary,
                    "artifact_refs": [profiles.as_posix()],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the trusted stage arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.request_json, args.profiles)


if __name__ == "__main__":
    raise SystemExit(main())
