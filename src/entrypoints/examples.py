"""``vibesys example``: list the example catalog and start a project from an example.

``vibesys example list [--json]`` prints every example in the checkout's
``examples/registry.toml`` by category, with its tasks; ``--json`` prints one
:class:`~entrypoints.example_catalog.ExampleList`.

``vibesys example new EXAMPLE DEST`` copies the example's tracked files into
``DEST`` (absent or an empty directory), makes ``DEST`` a Git repository with
one initial commit, and prints the ``vibesys --task ...`` line to run there.
An example whose source is another repository (``external_repo = true``) is
cloned at the commit the checkout pins instead. Runs never use the catalog in
place: an example inside the VibeSys checkout cannot be its own repository.

The file and Git work goes through :class:`ExampleMaterializer`;
:class:`GitExampleMaterializer` is the real one. This is a light host command:
its imports stay within ``tests/architecture/test_light_entrypoints.py``.
"""

from __future__ import annotations

import argparse
import configparser
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from entrypoints.example_catalog import (
    CatalogError,
    ExampleList,
    example_tasks,
    find_example,
    list_examples,
    load_registry,
)
from vs_project.api import Git
from vs_sim.api import OsProcesses

if TYPE_CHECKING:
    from collections.abc import Sequence


class ExampleMaterializationError(Exception):
    """A copy, clone, or commit failed; the message names the command or path."""


class ExampleMaterializer(Protocol):
    """Creates a standalone project from an example of a VibeSys checkout.

    ``destination`` is absent or an empty directory when either method is
    called. On success it is a Git repository holding the example; on failure
    the method raises :class:`ExampleMaterializationError` and the caller
    restores ``destination``.
    """

    def copy_tracked(
        self, checkout: Path, example: PurePosixPath, destination: Path, *, message: str
    ) -> None:
        """Copy the files ``checkout`` tracks under ``example`` and commit them as a new repository.

        Paths are relative to ``example``; submodules inside it are not copied.
        """
        ...

    def clone_pinned(self, checkout: Path, example: PurePosixPath, destination: Path) -> None:
        """Clone the repository ``checkout`` pins as the submodule ``example``, at the pinned commit."""
        ...


@dataclass(frozen=True)
class GitExampleMaterializer:
    """Uses the real file system, and starts Git through ``git``."""

    git: Git

    def copy_tracked(
        self, checkout: Path, example: PurePosixPath, destination: Path, *, message: str
    ) -> None:
        """Copy the tracked regular files and symlinks, then ``git init`` and commit."""
        listing = self._git(checkout, "ls-files", "--stage", "-z", "--", example.as_posix())
        prefix = f"{example.as_posix()}/"
        destination.mkdir(parents=True, exist_ok=True)
        for record in filter(None, listing.split("\0")):
            mode, _object, rest = record.split(" ", 2)
            path = rest.split("\t", 1)[1]
            if mode == _GITLINK_MODE or not path.startswith(prefix):
                continue
            source = checkout / path
            target = destination / path.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        self._git(destination, "init", "--quiet")
        self._git(destination, "add", "--all")
        self._git(destination, "commit", "--quiet", "--message", message)

    def clone_pinned(self, checkout: Path, example: PurePosixPath, destination: Path) -> None:
        """Clone the submodule's URL and branch, then point the branch at the pinned commit."""
        url, branch = _submodule_source(checkout, example)
        entry = self._git(checkout, "ls-tree", "HEAD", "--", example.as_posix()).strip()
        mode, _kind, rest = entry.split(" ", 2) if entry else ("", "", "")
        if mode != _GITLINK_MODE:
            message = f"{example} is not a submodule in {checkout}"
            raise ExampleMaterializationError(message)
        commit = rest.split("\t", 1)[0]
        clone = ["clone", "--quiet", *(["--branch", branch] if branch else []), url]
        self._git(checkout, *clone, str(destination))
        self._git(destination, "checkout", "--quiet", "-B", branch or "vibesys", commit)

    def _git(self, cwd: Path, *args: str) -> str:
        try:
            completed = self.git.run_text(args, cwd=cwd)
        except OSError as exc:
            message = f"cannot run git: {exc}"
            raise ExampleMaterializationError(message) from exc
        if completed.returncode != 0:
            message = f"`git {' '.join(args)}` failed in {cwd}: {completed.stderr.strip()}"
            raise ExampleMaterializationError(message)
        return completed.stdout


_GITLINK_MODE = "160000"


def _submodule_source(checkout: Path, example: PurePosixPath) -> tuple[str, str | None]:
    """Return the URL and branch ``.gitmodules`` declares for the submodule at ``example``."""
    config = configparser.ConfigParser()
    config.read(checkout / ".gitmodules", encoding="utf-8")
    for section in config.sections():
        if config.get(section, "path", fallback=None) == example.as_posix():
            return config.get(section, "url"), config.get(section, "branch", fallback=None)
    message = f"{checkout / '.gitmodules'} declares no submodule at {example}"
    raise ExampleMaterializationError(message)


def default_checkout() -> Path:
    """The VibeSys checkout this module runs from."""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class NewProject:
    """What ``example new`` created."""

    example: str
    destination: Path
    tasks: tuple[str, ...]

    def instructions(self) -> str:
        """The lines that tell the user how to start a run in the new project."""
        lines = [f"Created {self.destination} from example {self.example}.", "Run it with:"]
        lines.append(f"  cd {self.destination}")
        if self.tasks:
            lines.append(f"  vibesys --task {self.tasks[0]}")
        if len(self.tasks) > 1:
            lines.append(f"Other tasks: {', '.join(self.tasks[1:])}")
        return "\n".join(lines)


class DestinationError(Exception):
    """``example new`` refuses a destination that is not empty or lies inside the checkout."""


def new_example(
    checkout: Path, name: str, destination: Path, materializer: ExampleMaterializer
) -> NewProject:
    """Create ``destination`` from the example ``name``; nothing is left behind on failure."""
    root = checkout.expanduser().resolve()
    entry = find_example(load_registry(root), name)
    target = destination.expanduser().resolve()
    existed = target.exists()
    if existed and (not target.is_dir() or any(target.iterdir())):
        message = f"refusing to create a project in {target}: it exists and is not empty"
        raise DestinationError(message)
    if target.is_relative_to(root):
        message = f"refusing to create a project inside the VibeSys checkout: {target}"
        raise DestinationError(message)
    example = PurePosixPath(entry.path)
    try:
        if entry.external_repo:
            materializer.clone_pinned(root, example, target)
        else:
            materializer.copy_tracked(
                root, example, target, message=f"Start from VibeSys example {entry.name}"
            )
        tasks = example_tasks(target)
    except BaseException:
        _restore(target, existed=existed)
        raise
    return NewProject(example=entry.name, destination=target, tasks=tasks)


def _restore(target: Path, *, existed: bool) -> None:
    """Return ``target`` to absent, or to the empty directory it was."""
    if not target.exists():
        return
    if not existed:
        shutil.rmtree(target)
        return
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def format_examples(listing: ExampleList) -> str:
    """Render the catalog for a terminal: categories, then one example per line with its tasks."""
    lines: list[str] = []
    width = max((len(example.name) for example in listing.examples), default=0)
    category = None
    for example in listing.examples:
        if example.category != category:
            category = example.category
            lines.append(f"{category}:")
        tasks = ", ".join(example.tasks) or "(external repository, not fetched)"
        lines.append(f"  {example.name:<{width}}  {tasks}")
    return "\n".join(lines) if lines else "The catalog has no examples."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys example",
        description="List the example catalog or start a new project from an example.",
    )
    parser.add_argument(
        "--checkout",
        type=Path,
        default=None,
        help="the VibeSys source checkout whose examples/ to use (default: this one)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list examples by category with their tasks")
    listing.add_argument("--json", action="store_true", help="print one JSON document")
    new = commands.add_parser("new", help="copy an example into a new Git repository")
    new.add_argument("example", help="the example name `vibesys example list` shows")
    new.add_argument("destination", type=Path, help="an absent or empty directory")
    return parser


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    materializer: ExampleMaterializer | None = None,
    checkout: Path | None = None,
) -> tuple[int, str, str]:
    """Run one ``vibesys example`` command; return its exit code, stdout and stderr text.

    ``checkout`` is the default for ``--checkout``; ``materializer`` defaults to
    :class:`GitExampleMaterializer`.
    """
    args = _parser().parse_args(list(argv))
    root = args.checkout or checkout or default_checkout()
    if not root.is_absolute():
        root = cwd / root
    try:
        if args.command == "list":
            listing = list_examples(root)
            text = listing.model_dump_json() if args.json else format_examples(listing)
            return 0, text + "\n", ""
        destination = args.destination.expanduser()
        if not destination.is_absolute():
            destination = cwd / destination
        project = new_example(
            root,
            args.example,
            destination,
            materializer or GitExampleMaterializer(Git(OsProcesses())),
        )
    except (CatalogError, DestinationError, ExampleMaterializationError) as exc:
        return 1, "", f"vibesys example: {exc}\n"
    return 0, project.instructions() + "\n", ""


def main(argv: list[str] | None = None) -> int:
    """Run ``vibesys example`` from the current directory."""
    code, output, error = run(sys.argv[1:] if argv is None else argv, cwd=Path.cwd())
    sys.stdout.write(output)
    sys.stderr.write(error)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
