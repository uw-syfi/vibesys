"""The smoke a vibesys-evaluator wheel must pass in a clean environment with no Go toolchain.

Run with the installed package's interpreter (``python -I``), from outside the repository:

``evaluator_wheel_smoke.py --check VERSION PREFIX``
    The three binaries resolve to the wheel's ``_bin/`` inside ``PREFIX``, and the core and loadgen
    report ``VERSION`` (the Go module's semantic version the release stamps into them).
``evaluator_wheel_smoke.py benchmark --vs-output FILE``
    A task script: ``vibesys_evaluator.main`` runs one sample, which needs the core binary to write
    its result-protocol-2 records.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import vibesys_evaluator as vse
from vibesys_evaluator._private import core


def _check(version: str, prefix: str) -> int:
    if shutil.which("go") is not None:
        print("smoke: a Go toolchain is on PATH; this check needs none", file=sys.stderr)
        return 1
    root = Path(prefix).resolve()
    for name in core.ENV_VARS:
        found = Path(core.find_binary(name)).resolve()
        if not found.is_relative_to(root) or found.parent.name != "_bin":
            print(
                f"smoke: {name} resolves to {found}, not the wheel's _bin/ in {root}",
                file=sys.stderr,
            )
            return 1
    reply = core.call("core", "version")
    if reply.get("loadgen_version", reply.get("loadgenVersion")) != version:
        print(f"smoke: the core reports {reply}, not version {version}", file=sys.stderr)
        return 1
    # lint-waiver: LW-150631 [S603]; argv is the wheel's own loadgen.
    done = subprocess.run(  # noqa: S603
        [core.find_binary("loadgen"), "--capabilities", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    capabilities = json.loads(done.stdout.splitlines()[1])
    if capabilities["loadgenVersion"] != version:
        print(
            f"smoke: loadgen reports {capabilities['loadgenVersion']}, not {version}",
            file=sys.stderr,
        )
        return 1
    print(f"smoke: core and loadgen {version} from {root}")
    return 0


def _benchmark(run: vse.Run) -> None:
    run.report({"smoke_ops": 1.0})


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        sys.exit(_check(sys.argv[2], sys.argv[3]))
    vse.main(metrics={"smoke_ops": vse.Metric(unit="ops", direction="max")}, benchmark=_benchmark)
