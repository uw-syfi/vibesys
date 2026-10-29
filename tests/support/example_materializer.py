"""A Fake :class:`~entrypoints.examples.ExampleMaterializer` and the contract every one passes.

The Fake treats every regular file under an example as tracked and keeps the
repositories it "creates" in memory. It writes the copied files to the real
destination directory, so the shell under test sees the same tree it would
after a real copy, and it enforces the interface's precondition (an absent or
empty destination) as strictly as the contract requires.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from entrypoints.examples import ExampleMaterializationError, ExampleMaterializer

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class FakeRepository:
    """A repository the Fake created: how, and the files its one commit holds."""

    origin: str
    """The commit message of a copy, or ``clone <example>`` for a clone."""
    files: frozenset[str]


@dataclass
class FakeExampleMaterializer:
    """In-memory Git over the real file system.

    ``pinned`` maps each external example to the files of its pinned commit.
    ``fail_after`` injects a failure after that many files were written.
    """

    pinned: Mapping[PurePosixPath, Mapping[str, str]] = field(default_factory=dict)
    fail_after: int | None = None
    repositories: dict[Path, FakeRepository] = field(default_factory=dict)

    def copy_tracked(
        self, checkout: Path, example: PurePosixPath, destination: Path, *, message: str
    ) -> None:
        """Copy every regular file under the example, then record one commit."""
        _require_free(destination)
        source = checkout / example
        files = {
            path.relative_to(source).as_posix(): path
            for path in sorted(source.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(source).parts
        }
        destination.mkdir(parents=True, exist_ok=True)
        for written, (relative, path) in enumerate(files.items()):
            self._maybe_fail(written)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        self.repositories[destination] = FakeRepository(message, frozenset(files))

    def clone_pinned(self, checkout: Path, example: PurePosixPath, destination: Path) -> None:
        """Write the pinned commit's files, as a clone would."""
        _require_free(destination)
        files = self.pinned.get(example)
        if files is None:
            message = f"{checkout / '.gitmodules'} declares no submodule at {example}"
            raise ExampleMaterializationError(message)
        destination.mkdir(parents=True, exist_ok=True)
        for written, (relative, text) in enumerate(sorted(files.items())):
            self._maybe_fail(written)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.repositories[destination] = FakeRepository(f"clone {example}", frozenset(files))

    def repository_root(self, path: Path) -> Path | None:
        """The nearest directory at or above ``path`` holding ``.git``, as Git would find it."""
        resolved = path.resolve()
        return next(
            (folder for folder in (resolved, *resolved.parents) if (folder / ".git").exists()),
            None,
        )

    def _maybe_fail(self, written: int) -> None:
        if self.fail_after is not None and written >= self.fail_after:
            message = f"injected failure after {written} files"
            raise ExampleMaterializationError(message)


def _require_free(destination: Path) -> None:
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        message = f"destination is not empty: {destination}"
        raise ExampleMaterializationError(message)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def check_copy_tracked_contract(
    materializer: ExampleMaterializer, checkout: Path, example: PurePosixPath, tmp: Path
) -> None:
    """Contract: a copy holds exactly the example's tracked files, into absent or empty dirs.

    ``checkout`` must track every file under ``example`` and nothing else
    there, so the Fake's "every file is tracked" and Git agree.
    """
    expected = _tree(checkout / example)
    assert expected, "the contract needs an example with files"

    absent = tmp / "absent"
    materializer.copy_tracked(checkout, example, absent, message="start")
    assert _tree(absent) == expected

    empty = tmp / "empty"
    empty.mkdir()
    materializer.copy_tracked(checkout, example, empty, message="start")
    assert _tree(empty) == expected


def check_repository_root_contract(
    materializer: ExampleMaterializer, repository: Path, outside: Path
) -> None:
    """Contract: a folder inside ``repository`` names its root; one outside any names none.

    ``repository`` must be a Git repository with a ``nested`` folder, and ``outside`` a
    directory in no repository.
    """
    nested = repository / "nested"
    assert materializer.repository_root(nested) == repository.resolve()
    assert materializer.repository_root(repository) == repository.resolve()
    assert materializer.repository_root(outside) is None
