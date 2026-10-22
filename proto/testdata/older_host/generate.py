"""Record how a real older VibeSys checkout answers every host command the gateway runs.

Usage: python3 proto/testdata/older_host/generate.py <older-checkout>

<older-checkout> is a checkout of this repository from before the registry commands, with its
virtualenv built (`uv sync`). Each command is run as `<checkout>/.venv/bin/vibesys host <command>`
with an empty stdin, exactly as the gateway runs it. The output is committed as
`older_host.json`, which the gateway's classifier and the client's older-host Fake are both asserted
against, so neither test runs git or a subprocess. The recorded stderr names no path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OPERATIONS = ROOT / "gateway" / "internal" / "hostoperations"
USAGE_STATUS = 2


def commands() -> list[str]:
    """Every host command the gateway can run, from its generated dispatch tables."""
    found: set[str] = set()
    for table in OPERATIONS.glob("host_*_generated.go"):
        found.update(re.findall(r'CLI: "([a-z-]+)"', table.read_text()))
    return sorted(found)


def record(checkout: Path, command: str) -> dict[str, object]:
    done = subprocess.run(
        [str(checkout / ".venv" / "bin" / "vibesys"), "host", command],
        input=b"",
        capture_output=True,
        check=False,
        cwd=checkout,
    )
    stderr = done.stderr.decode()
    if str(checkout) in stderr:
        raise SystemExit(f"{command}: stderr names the checkout path")
    return {
        "command": command,
        "exit_code": done.returncode,
        "stdout_bytes": len(done.stdout),
        "stderr": stderr,
    }


def main() -> None:
    checkout = Path(sys.argv[1]).resolve()
    revision = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--short=9", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    runs = [record(checkout, command) for command in commands()]
    corpus = {
        "checkout": revision,
        "rejected": [run for run in runs if run["exit_code"] == USAGE_STATUS],
        "provided": [run["command"] for run in runs if run["exit_code"] != USAGE_STATUS],
    }
    target = Path(__file__).with_name("older_host.json")
    target.write_text(json.dumps(corpus, indent=2) + "\n")


if __name__ == "__main__":
    main()
