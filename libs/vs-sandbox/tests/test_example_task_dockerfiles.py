"""The example tasks' pinned Dockerfiles stay in step with the toolchain catalog and each other.

Each CPU example task ships a self-contained Dockerfile, and the Dockerfiles repeat facts
that live elsewhere: the Rust and Go versions VibeSys layers onto every run image
(:mod:`vs_sandbox.toolchain_features`), and the engine commit the differential-dataflow
task materializes (its ``vibesys.input.toml``). Nothing else holds those copies together,
so this reads the files in the repository and checks them. It reads tracked files only and
never needs a git submodule to be fetched.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from vs_sandbox.toolchain_features import GO_TOOLCHAIN_VERSION, RUST_TOOLCHAIN_VERSION

_REPOSITORY = Path(__file__).resolve().parents[3]
# The Verus Dockerfile is left out: it carries its own Rust and picks its platform on purpose,
# because Verus ships binaries for one platform.
_TASK_DOCKERFILES = sorted(
    path
    for path in (_REPOSITORY / "examples").glob("**/.vibesys/tasks/*/Dockerfile")
    if path.parent.name != "verus-mpmc-open"
)
_DIFFERENTIAL_DATAFLOW = (
    _REPOSITORY / "examples/database/differential-dataflow/.vibesys/tasks/default"
)


def _arg(dockerfile: Path, name: str) -> str | None:
    """The default value of ``ARG name=...`` in *dockerfile*, or ``None`` if it declares none."""
    found = re.search(rf"^ARG {name}=(\S+)$", dockerfile.read_text(encoding="utf-8"), re.MULTILINE)
    return None if found is None else found.group(1)


def _relative(dockerfile: Path) -> str:
    return dockerfile.relative_to(_REPOSITORY).as_posix()


def test_the_example_tasks_are_found() -> None:
    assert _TASK_DOCKERFILES, "no example task Dockerfile found; the glob or the layout changed"


def test_the_rust_and_go_pins_equal_the_catalog() -> None:
    rust = [d for d in _TASK_DOCKERFILES if _arg(d, "RUST_TOOLCHAIN") is not None]
    go = [d for d in _TASK_DOCKERFILES if _arg(d, "GO_VERSION") is not None]

    assert rust, "no Dockerfile pins Rust"
    assert go, "no Dockerfile pins Go"
    assert {_relative(d): _arg(d, "RUST_TOOLCHAIN") for d in rust} == {
        _relative(d): RUST_TOOLCHAIN_VERSION for d in rust
    }
    assert {_relative(d): _arg(d, "GO_VERSION") for d in go} == {
        _relative(d): GO_TOOLCHAIN_VERSION for d in go
    }


def test_the_rust_and_go_dockerfiles_are_identical() -> None:
    family = [
        d
        for d in _TASK_DOCKERFILES
        if _arg(d, "RUST_TOOLCHAIN") is not None
        and _arg(d, "GO_VERSION") is not None
        and _arg(d, "RUST_NIGHTLY") is None
    ]

    assert family, "no Rust + Go Dockerfile found"
    assert len({d.read_bytes() for d in family}) == 1, (
        f"the {len(family)} Rust + Go Dockerfiles differ; edit them together"
    )


def test_the_engine_commit_equals_the_manifest_commit() -> None:
    manifest = tomllib.loads(
        (_DIFFERENTIAL_DATAFLOW / "vibesys.input.toml").read_text(encoding="utf-8")
    )
    sources = manifest["workspace"]["sources"]

    commits = {source["commit"] for source in sources}
    repos = {source["repo"] for source in sources}

    assert len(commits) == 1, f"the engine sources pin different commits: {sorted(commits)}"
    assert _arg(_DIFFERENTIAL_DATAFLOW / "Dockerfile", "ENGINE_COMMIT") == next(iter(commits))
    assert _arg(_DIFFERENTIAL_DATAFLOW / "Dockerfile", "ENGINE_REPO") == next(iter(repos))


def test_the_dockerfiles_do_not_pin_a_platform() -> None:
    pinned = [
        _relative(d)
        for d in _TASK_DOCKERFILES
        if re.search(r"^FROM\s+--platform=", d.read_text(encoding="utf-8"), re.MULTILINE)
    ]

    assert pinned == [], (
        "these Dockerfiles force one architecture; the pinned base digests are multi-arch "
        f"image indexes, so let the build's platform decide: {pinned}"
    )
