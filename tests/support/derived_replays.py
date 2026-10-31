"""The native replays the clients test against, generated from their declarations, never committed.

A replay is the shipping records projector applied to a declaration, so it is a function of the
projector, the schemas and the declaration. Committing it made every pull request that touched any
of them rewrite the same dozens of files, and two such pull requests always conflicted. Here a set is
stored under the hash of everything it derives from, so a changed input names a different set and a
stale one cannot be read.

``INPUTS`` lists the inputs by path. ``test_derived_replay_inputs_cover_every_file_that_runs_while_projecting``
holds the list complete: it fails, naming the file, when projection runs code outside it. The key is
computed from file bytes alone, so a CI step can ask for it before any environment exists.

``materialize`` publishes the set a key names with one rename, so a killed run leaves nothing a
reader could take for a complete set, and returns its directory. Two layouts live in it:
``shared/<name>.pb`` from the run-fixtures declarations and ``journeys/<name>.pb`` from the desktop
shell scenarios. Generation takes minutes for the two 600 idea scenarios, so it runs in a step
before the tests (``clients/tools/scripts/with_derived_replays.mjs``), never inside one.
Only the standard library is imported, so the module is cheap to load from a script or a CI step.
``materialize_replays`` holds the generators and the command that starts them.

``python -m tests.support.derived_replays key`` prints the key and the cache directory as
``name=value`` lines, the GitHub output format. ``path`` prints the directory of the set the
current inputs name, and exits 1 when it has not been made.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
#: Overrides the cache directory; otherwise ``$XDG_CACHE_HOME/vibesys/derived-replays``.
CACHE_VARIABLE = "VIBESYS_DERIVED_REPLAYS_CACHE"
#: Sets kept per machine, newest use first. A set is 8 MB, and one is made per input change.
KEEP = 8
SUFFIXES = frozenset({".py", ".proto", ".json"})
#: What a replay derives from, as repository paths: a file, or a directory read whole. The
#: schemas and their bindings, the run log fold, the projector and the code it runs in other
#: packages, the generators and the declarations they read.
INPUTS = (
    "proto/vibesys",
    "libs/vs-proto/src/vs_proto",
    "libs/vs-runlog/src/vs_runlog",
    "libs/vs-decode/src/vs_decode",
    "libs/vs-redaction/src/vs_redaction",
    "libs/vs-core/src/vs_core/_values.py",
    "libs/vs-core/src/vs_core/flows/_spans.py",
    "libs/vs-core/src/vs_core/types/common.py",
    "src/vibesys/events.py",
    "src/vibesys/metrics.py",
    "src/vibesys/records",
    "src/vibesys/search_spans",
    "tests/support/derived_replays.py",
    "tests/support/journey_fixtures.py",
    "tests/support/lineage_fixture.py",
    "tests/support/materialize_replays.py",
    "tests/support/retained_events.py",
    "tests/support/shared_scenarios.py",
    "clients/libs/run-fixtures/src/data",
    "clients/apps/desktop-shell/e2e/scenarios",
)

#: Writes a set into an empty directory.
Produce = Callable[[Path], None]


def input_files(repository: Path = REPOSITORY, inputs: Sequence[str] = INPUTS) -> list[Path]:
    """Every file the inputs name, in path order; a missing input is an error naming it."""
    files: set[Path] = set()
    for name in inputs:
        path = repository / name
        if path.is_file():
            files.add(path)
        elif path.is_dir():
            files.update(
                found
                for found in path.rglob("*")
                if found.is_file() and found.suffix in SUFFIXES and "__pycache__" not in found.parts
            )
        else:
            message = f"derived replay input {name} does not exist; fix INPUTS in {__file__}"
            raise FileNotFoundError(message)
    return sorted(files)


def covered(path: Path, repository: Path = REPOSITORY, inputs: Sequence[str] = INPUTS) -> bool:
    """Whether a change to ``path`` changes the key."""
    relative = path.resolve().relative_to(repository)
    if relative.suffix not in SUFFIXES:
        return False
    return any(relative == Path(name) or Path(name) in relative.parents for name in inputs)


def key(repository: Path = REPOSITORY, inputs: Sequence[str] = INPUTS) -> str:
    """One hash over the relative name and the bytes of each input file, in path order."""
    digest = hashlib.sha256()
    for path in input_files(repository, inputs):
        digest.update(path.relative_to(repository).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def cache_directory(environment: Mapping[str, str] = os.environ) -> Path:
    """Where sets are kept, shared by every checkout on the machine."""
    if override := environment.get(CACHE_VARIABLE):
        return Path(override)
    home = environment.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(home) / "vibesys" / "derived-replays"


def find(*, cache: Path, set_key: str) -> Path | None:
    """The directory of the set ``set_key`` names, or None when the cache lacks it."""
    published = cache / set_key
    if not published.is_dir():
        return None
    published.touch()
    return published


def materialize(*, cache: Path, set_key: str, produce: Produce) -> Path:
    """The directory of the set ``set_key`` names, generated first when the cache lacks it.

    The set is written beside its final place and renamed into it, so the directory exists only
    when it is complete. A producer that raises, or writes no file, publishes nothing. Two
    processes that make the same set both succeed; the second discards its copy.
    """
    cache.mkdir(parents=True, exist_ok=True)
    if (found := find(cache=cache, set_key=set_key)) is not None:
        return found
    staging = Path(tempfile.mkdtemp(prefix=f".{set_key}-", dir=cache))
    try:
        produce(staging)
        if not any(staging.rglob("*.pb")):
            message = f"generating the derived replays for {set_key} wrote no .pb file"
            raise RuntimeError(message)
        published = cache / set_key
        try:
            staging.rename(published)
        except OSError:
            if not published.is_dir():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _prune(cache, keep=published)
    return published


def _prune(cache: Path, *, keep: Path) -> None:
    """Remove the sets used least recently beyond ``KEEP``, and never ``keep``."""
    sets = sorted(
        (path for path in cache.iterdir() if path.is_dir() and not path.name.startswith(".")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in sets[KEEP:]:
        if path != keep:
            shutil.rmtree(path, ignore_errors=True)


def main(arguments: Sequence[str]) -> int:
    """The command line: ``key`` or ``path``."""
    if list(arguments) == ["key"]:
        sys.stdout.write(f"key={key()}\npath={cache_directory()}\n")
        return 0
    if list(arguments) == ["path"]:
        found = find(cache=cache_directory(), set_key=key())
        if found is None:
            return 1
        sys.stdout.write(f"{found}\n")
        return 0
    sys.stderr.write("usage: python -m tests.support.derived_replays key | path\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
