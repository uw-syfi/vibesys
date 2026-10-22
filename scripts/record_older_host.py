"""Record how a real older VibeSys checkout answers every host command the gateway runs.

Usage: python3 scripts/record_older_host.py <older-checkout> <revision>

<older-checkout> is a checkout of this repository from before the registry commands, with its
virtualenv built (`uv sync`), and <revision> names the commit it is at. Each command runs as
`<checkout>/.venv/bin/vibesys host <command>` with an empty stdin, exactly as the gateway runs
it. The output is committed as `proto/testdata/older_host/older_host.json`, which the gateway's
classifier and the client's older-host Fake are both asserted against, so neither test runs git
or a subprocess. The recorded stderr names no path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "gateway" / "internal" / "hostoperations"
CORPUS = ROOT / "proto" / "testdata" / "older_host" / "older_host.json"
USAGE_STATUS = 2


def commands() -> list[str]:
    """Every host command the gateway can run, from its generated dispatch tables."""
    found: set[str] = set()
    for table in OPERATIONS.glob("host_*_generated.go"):
        found.update(re.findall(r'CLI: "([a-z-]+)"', table.read_text()))
    return sorted(found)


def record(checkout: Path, command: str) -> dict[str, object]:
    """Run one command on the older checkout and keep what it printed."""
    # lint-waiver: LW-317002 [S603]; the executable is the older checkout's own, named by the caller.
    # > A wrapper adds indirection around the one call this script exists to make; the argv is
    # > fixed (`host <command>` from the gateway's own tables), runs without a shell, and a
    # > recording must execute the checkout, so no executable can be a literal.
    done = subprocess.run(  # noqa: S603
        [str(checkout / ".venv" / "bin" / "vibesys"), "host", command],
        input=b"",
        capture_output=True,
        check=False,
        cwd=checkout,
    )
    stderr = done.stderr.decode()
    if str(checkout) in stderr:
        sys.exit(f"{command}: stderr names the checkout path")
    return {
        "command": command,
        "exit_code": done.returncode,
        "stdout_bytes": len(done.stdout),
        "stderr": stderr,
    }


def main() -> None:
    """Write the corpus."""
    checkout = Path(sys.argv[1]).resolve()
    runs = [record(checkout, command) for command in commands()]
    corpus = {
        "checkout": sys.argv[2],
        "rejected": [run for run in runs if run["exit_code"] == USAGE_STATUS],
        "provided": [run["command"] for run in runs if run["exit_code"] != USAGE_STATUS],
    }
    CORPUS.write_text(json.dumps(corpus, indent=2) + "\n")


if __name__ == "__main__":
    main()
