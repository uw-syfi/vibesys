"""Which committed wire goldens a change to the schemas or to their inputs made stale.

Two golden sets are not native replays, so ``native_goldens`` does not cover them:

- ``proto/testdata/conversion`` is a real run converted by ``generate.py``. Its inputs are the
  ``.proto`` files, the records projector, the run log package, the generator and the retained
  usage capture it replays.
- ``proto/testdata/skeleton-wire`` is the gateway's Skeleton reply over the conversion goldens,
  written by the gateway test. Its inputs are the ``.proto`` files and the conversion goldens.
  The gateway code that builds the reply is not one: the gateway test compares it byte for byte.

Each directory holds a ``source.sha256`` with the hash of its inputs, written by
``python -m tests.support.wire_goldens`` as the last step of ``scripts/regenerate_goldens.sh``.
A stale hash is found from file bytes alone, in milliseconds, so the proto check group can fail
before any test converts a run. Only the standard library is imported, so the module is cheap
to load from a script or a check.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
#: The one command that rewrites every golden a records projector or ``.proto`` change invalidates.
REGENERATE = "./scripts/regenerate_goldens.sh"
FINGERPRINT = "source.sha256"
CONVERSION = "proto/testdata/conversion"
SKELETON_WIRE = "proto/testdata/skeleton-wire"
_USAGE_CAPTURE = "tests/conformance/fixtures/usage-records/codex-ten-rounds-trimmed.json"


def conversion_inputs(repository: Path = REPOSITORY) -> list[Path]:
    """The files the conversion goldens derive from."""
    paths = sorted((repository / "proto/vibesys").rglob("*.proto"))
    paths += sorted((repository / "src/vibesys/records").glob("*.py"))
    paths += sorted((repository / "libs/vs-runlog/src/vs_runlog").glob("*.py"))
    paths += [repository / CONVERSION / "generate.py", repository / _USAGE_CAPTURE]
    return paths


def skeleton_wire_inputs(repository: Path = REPOSITORY) -> list[Path]:
    """The files the Skeleton reply derives from: the schemas and the converted run it reads."""
    paths = sorted((repository / "proto/vibesys").rglob("*.proto"))
    paths += sorted(
        path
        for path in (repository / CONVERSION).rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc"} and path.name != FINGERPRINT
    )
    return paths


def fingerprint(paths: list[Path], repository: Path = REPOSITORY) -> str:
    """One hash over the relative name and the bytes of each path, in the order given."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(repository).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def stale_wire_goldens(repository: Path = REPOSITORY) -> list[str]:
    """One line per wire golden set whose recorded input hash differs from the current one."""
    problems = []
    for directory, inputs in _sets(repository):
        current = fingerprint(inputs, repository)
        path = repository / directory / FINGERPRINT
        label = f"{directory}/{FINGERPRINT}"
        if not path.exists():
            problems.append(f"{label} is missing")
        elif (recorded := path.read_text().strip()) != current:
            problems.append(
                f"{label} records golden inputs {recorded[:12]}, which now hash to {current[:12]}"
            )
    return problems


def record(repository: Path = REPOSITORY) -> list[str]:
    """Write the current input hash beside each set; the files written."""
    written = []
    for directory, inputs in _sets(repository):
        (repository / directory / FINGERPRINT).write_text(fingerprint(inputs, repository) + "\n")
        written.append(f"{directory}/{FINGERPRINT}")
    return written


def _sets(repository: Path) -> tuple[tuple[str, list[Path]], ...]:
    return (
        (CONVERSION, conversion_inputs(repository)),
        (SKELETON_WIRE, skeleton_wire_inputs(repository)),
    )


if __name__ == "__main__":
    sys.stdout.write("".join(f"recorded {name}\n" for name in record()))
