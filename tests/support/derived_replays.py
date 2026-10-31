"""The native replays the clients test against, generated from their declarations, never committed.

A replay is the shipping records projector applied to a declaration, so it is a function of the
projector, the schemas, the libraries and interpreter that run it, and the declaration. Committing
it made every pull request that touched any of them rewrite the same dozens of files, and two such
pull requests always conflicted. Here a set is stored under a hash of everything it can derive
from, so a changed input names a different set and a stale one cannot be read.

The key is coarse on purpose. It hashes every file under the roots of the ``derived_replays``
component in ``.repoctl/components.toml`` (whole source packages the generators reach through
imports, not the functions a trace saw run), the files that component lists (the lock file, the
project files, the generators), and the Python version. Regenerating takes seconds, so the key
errs wide rather than listing what matters. CI selects the client jobs on a change to the same
component, so there is one list. Each entry of it may say ``selects = false``: it still keys the
set, but a change to it selects no job (the libraries the projection only imports).

``materialize`` publishes the set a key names with one rename, so a killed run leaves nothing a
reader could take for a complete set, and refuses to publish when an input changed while it ran.
It returns the set's directory. The layouts in it: ``shared/<name>.pb`` from the run-fixtures
declarations, ``journeys/<name>.pb`` from the desktop shell scenarios, and ``variants/``, where
the desktop e2e keeps the replay of a fixture that differs from its declaration. A run starts
through ``clients/tools/scripts/with_derived_replays.mjs``, which generates the set before the
tests, never inside one.

Only the standard library is imported, so the module is cheap to load from a script or a CI step.
``materialize_replays`` holds the generators and the command that starts them.
``python -m tests.support.derived_replays key`` prints the key and the cache directory as
``name=value`` lines, the GitHub output format; ``path`` prints the directory of the set the
current inputs name, and exits 1 when it has not been made.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
#: The policy that lists the inputs, and the component in it that does.
POLICY = ".repoctl/components.toml"
COMPONENT = "derived_replays"
#: Overrides the cache directory; otherwise ``$XDG_CACHE_HOME/vibesys/derived-replays``.
CACHE_VARIABLE = "VIBESYS_DERIVED_REPLAYS_CACHE"
#: Sets kept per machine, newest use first. A set is 8 MB, and one is made per input change.
KEEP = 8
#: A set used within this many seconds of the newest one is never pruned, so a long run on an
#: older checkout keeps reading what it started with.
RETAIN_SECONDS = 24 * 60 * 60
_JUNK_PARTS = frozenset({"__pycache__", "node_modules"})
_JUNK_SUFFIXES = frozenset({".pyc", ".pyo", ".so", ".dylib"})

#: Writes a set into an empty directory.
Produce = Callable[[Path], None]
#: The key of the set the inputs name now.
KeyOf = Callable[[], str]


def _entries(repository: Path) -> list[tuple[str, bool]]:
    """The component's roots then files as (path, selects): a string selects, a table says."""
    components = tomllib.loads((repository / POLICY).read_text()).get("components", [])
    named = [component for component in components if component.get("id") == COMPONENT]
    if len(named) != 1:
        message = (
            f"{POLICY} must declare exactly one component with id {COMPONENT!r}; found {len(named)}"
        )
        raise ValueError(message)
    entries: list[tuple[str, bool]] = []
    for entry in (*named[0].get("roots", ()), *named[0].get("files", ())):
        if isinstance(entry, str):
            entries.append((entry, True))
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entries.append((entry["path"], entry.get("selects", True) is not False))
        else:
            message = f"{POLICY}: a {COMPONENT} entry must be a string or a table with a path, got {entry!r}"
            raise TypeError(message)
    return entries


def inputs(repository: Path = REPOSITORY) -> tuple[str, ...]:
    """The key's inputs: every root and file of the ``derived_replays`` component."""
    return tuple(path for path, _ in _entries(repository))


def selecting_inputs(repository: Path = REPOSITORY) -> tuple[str, ...]:
    """The inputs a change to which also selects the client jobs: those not marked ``selects = false``."""
    return tuple(path for path, selects in _entries(repository) if selects)


def input_files(repository: Path = REPOSITORY, names: Sequence[str] | None = None) -> list[Path]:
    """Every file under the inputs, in path order; a missing input is an error naming it."""
    files: set[Path] = set()
    for name in inputs(repository) if names is None else names:
        path = repository / name
        if path.is_file():
            files.add(path)
        elif path.is_dir():
            files.update(found for found in path.rglob("*") if found.is_file() and not _junk(found))
        else:
            message = f"derived replay input {name} does not exist; fix the {COMPONENT} component in {POLICY}"
            raise FileNotFoundError(message)
    return sorted(files)


def key(
    repository: Path = REPOSITORY,
    names: Sequence[str] | None = None,
    python: Sequence[int] = tuple(sys.version_info[:3]),
) -> str:
    """One hash over the Python version and the relative name and bytes of each input file."""
    digest = hashlib.sha256()
    digest.update(f"python {'.'.join(map(str, python))} {sys.implementation.name}\0".encode())
    for path in input_files(repository, names):
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


def materialize(*, cache: Path, current_key: KeyOf, produce: Produce) -> Path:
    """The directory of the set the inputs name, generated first when the cache lacks it.

    The set is written beside its final place and renamed into it, so the directory exists only
    when it is complete. A producer that raises, or writes no file, publishes nothing, and neither
    does a run during which an input changed: the set would mix two versions under one key. Two
    processes that make the same set both succeed; the second discards its copy.
    """
    cache.mkdir(parents=True, exist_ok=True)
    set_key = current_key()
    if (found := find(cache=cache, set_key=set_key)) is not None:
        return found
    staging = Path(tempfile.mkdtemp(prefix=f".{set_key}-", dir=cache))
    try:
        produce(staging)
        if not any(staging.rglob("*.pb")):
            message = f"generating the derived replays for {set_key} wrote no .pb file"
            raise RuntimeError(message)
        if (after := current_key()) != set_key:
            message = (
                f"an input of the derived replays changed while they were generated "
                f"({set_key} became {after}); nothing was published, run again"
            )
            raise RuntimeError(message)
        (staging / "variants").mkdir(exist_ok=True)
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


def _junk(path: Path) -> bool:
    return (
        path.suffix in _JUNK_SUFFIXES
        or path.name == ".DS_Store"
        or any(part in _JUNK_PARTS or part.endswith(".egg-info") for part in path.parts)
    )


def _prune(cache: Path, *, keep: Path) -> None:
    """Remove the sets used least recently beyond ``KEEP``, except ``keep`` and recent ones."""
    sets = sorted(
        (path for path in cache.iterdir() if path.is_dir() and not path.name.startswith(".")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    now = keep.stat().st_mtime
    for path in sets[KEEP:]:
        if path != keep and now - path.stat().st_mtime >= RETAIN_SECONDS:
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
