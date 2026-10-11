"""Private source snapshots for real distribution builds in the test suite.

Packaging backends may create and remove staging directories beside the source
tree. Tests therefore copy the checked-out bytes first and build only inside
that test-owned copy. Concurrent packaging tests remain readers of the real
checkout and never race another test traversing it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from tests.support import run_test_command

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Protocol

    class _TextCommandResult(Protocol):
        """The build status exposed to packaging contract tests."""

        returncode: int
        stdout: str
        stderr: str


_PRIVATE_SOURCE_OWNER = object()


class PrivatePackagingSource:
    """An opaque capability for one test-owned packaging source tree."""

    __slots__ = ("__root", "__weakref__")

    def __init__(self, root: Path, owner: object | None = None) -> None:
        """Construct only for the module owner; callers receive issued values."""
        if owner is not _PRIVATE_SOURCE_OWNER:
            raise PrivatePackagingSourceError.not_owned()
        self.__root = root

    @property
    def root(self) -> Path:
        """The exact private tree bound to this capability when it was issued."""
        return self.__root


# Authority is the issued object's identity paired with its original root. A
# copied capability is a different object, and mutating an issued object's
# private slot makes its visible root disagree with the issuer's record.
_PRIVATE_SOURCE_ROOTS: WeakKeyDictionary[PrivatePackagingSource, Path] = WeakKeyDictionary()


class PrivatePackagingSourceError(ValueError):
    """Raised when packaging output could mutate the source being copied."""

    def __init__(self, detail: str) -> None:
        """Name the required ownership relationship."""
        super().__init__(f"private packaging source {detail}")

    @classmethod
    def nested_in_source(cls) -> PrivatePackagingSourceError:
        """Reject a snapshot that its own traversal could discover."""
        return cls("must be outside the source tree")

    @classmethod
    def not_owned(cls) -> PrivatePackagingSourceError:
        """Reject a value not issued by the private-source owner."""
        return cls("must be created by copy_private_packaging_source")


def copy_private_packaging_source(source_root: Path, destination: Path) -> PrivatePackagingSource:
    """Copy current source bytes into a private packaging tree.

    Git's tracked manifest selects the files, while their current working-tree
    bytes are copied. Modified and staged files are therefore inputs, but
    untracked caches, credentials, and build-backend output are not.
    """
    source_root = source_root.resolve()
    destination = destination.resolve()
    if destination == source_root or destination.is_relative_to(source_root):
        raise PrivatePackagingSourceError.nested_in_source()
    destination.mkdir()
    for relative in _tracked_source_paths(source_root):
        source_path = source_root / relative
        if not source_path.is_file() and not source_path.is_symlink():
            # Gitlinks name submodule directories rather than files. None of
            # the root distribution inputs come from those separate repos.
            continue
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path, follow_symlinks=False)
    private = PrivatePackagingSource(destination, _PRIVATE_SOURCE_OWNER)
    _PRIVATE_SOURCE_ROOTS[private] = destination
    return private


def build_distribution(
    source: PrivatePackagingSource,
    output: Path,
    *,
    artifact: Literal["wheel", "sdist"],
    environment: Mapping[str, str] | None = None,
    check: bool = True,
) -> _TextCommandResult:
    """Build one distribution artifact inside its private source snapshot."""
    if _PRIVATE_SOURCE_ROOTS.get(source) != source.root:
        raise PrivatePackagingSourceError.not_owned()
    return run_test_command(
        ["uv", "build", f"--{artifact}", "--out-dir", str(output)],
        cwd=source.root,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
    )


def _tracked_source_paths(source_root: Path) -> tuple[Path, ...]:
    result = run_test_command(
        ["git", "-C", str(source_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return tuple(Path(os.fsdecode(raw)) for raw in result.stdout.split(b"\0") if raw)
