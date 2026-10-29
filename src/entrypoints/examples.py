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
    example_tasks,
    find_example,
    list_examples,
    load_registry,
)
from server.host_api import HelperErrorCode
from server.host_examples import CreatedProject, ExampleDestination, ProjectLookupError
from vs_project.api import Git
from vs_sim.api import OsProcesses

if TYPE_CHECKING:
    from collections.abc import Sequence

    from server.host_examples import ExampleList


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

    def repository_root(self, path: Path) -> Path | None:
        """The root of the Git repository ``path`` is in, or ``None`` when it is in none."""
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

    def repository_root(self, path: Path) -> Path | None:
        """``git rev-parse --show-toplevel`` in ``path``; ``None`` outside any repository."""
        try:
            completed = self.git.run_text(("rev-parse", "--show-toplevel"), cwd=path)
        except OSError:
            return None
        top = completed.stdout.strip()
        return Path(top).resolve() if completed.returncode == 0 and top else None

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
    """A new project's destination is refused; the subclass says why and the message names it."""


class DestinationExistsError(DestinationError):
    """The destination exists (for an example, and is not an empty directory)."""


class DestinationNestedError(DestinationError):
    """The destination lies inside the VibeSys checkout or another Git repository."""


class NotNestedError(Exception):
    """A folder to copy out is in no other Git repository; it can run where it is."""


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
        raise DestinationExistsError(message)
    if target.is_relative_to(root):
        message = f"refusing to create a project inside the VibeSys checkout: {target}"
        raise DestinationNestedError(message)
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


def example_destination(
    checkout: Path, name: str, destination: Path
) -> tuple[ExampleDestination, Path | None]:
    """What :func:`new_example` would find at ``destination``, and the project there if it is one.

    A project is the example's when its tasks are the example's; an example whose source is
    another repository that is not fetched has none to compare, so nothing is its project.
    """
    root = checkout.expanduser().resolve()
    entry = find_example(load_registry(root), name)
    target = destination.expanduser().resolve()
    if not target.exists() and not target.is_symlink():
        return ExampleDestination.ABSENT, None
    if not target.is_dir():
        return ExampleDestination.OCCUPIED, None
    if not any(target.iterdir()):
        return ExampleDestination.EMPTY, None
    wanted = example_tasks(root / entry.path)
    try:
        found = example_tasks(target)
    except CatalogError:
        found = ()
    if wanted and found == wanted:
        return ExampleDestination.EXAMPLE_PROJECT, target
    return ExampleDestination.OCCUPIED, None


def copy_out(source: Path, destination: Path, materializer: ExampleMaterializer) -> NewProject:
    """Copy ``source``'s tracked files into their own new Git repository at ``destination``.

    ``source`` must sit inside another repository (a project there cannot be its own
    repository); ``destination`` must not exist, nor lie inside that repository. Nothing
    is left behind on failure.
    """
    folder = source.expanduser().resolve()
    if not folder.is_dir():
        message = f"no folder to copy out at {folder}"
        raise FileNotFoundError(message)
    repository = materializer.repository_root(folder)
    if repository is None or repository == folder:
        message = f"{folder} is in no other Git repository; run it where it is"
        raise NotNestedError(message)
    target = destination.expanduser().resolve()
    if target.exists() or target.is_symlink():
        message = f"refusing to copy out to {target}: it exists"
        raise DestinationExistsError(message)
    if target.is_relative_to(repository):
        message = f"refusing to copy out inside the repository {repository}: {target}"
        raise DestinationNestedError(message)
    relative = PurePosixPath(folder.relative_to(repository).as_posix())
    try:
        materializer.copy_tracked(
            repository, relative, target, message=f"Copy {relative} out of {repository.name}"
        )
        tasks = example_tasks(target)
    except BaseException:
        _restore(target, existed=False)
        raise
    return NewProject(example=relative.as_posix(), destination=target, tasks=tasks)


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


@dataclass(frozen=True)
class HelperExampleProjects:
    """The host helper's ``ExampleProjects``: this checkout's catalog, through ``materializer``.

    ``checkout`` is ``None`` for an installed distribution, which has no catalog.
    """

    checkout: Path | None
    materializer: ExampleMaterializer

    def list(self, *, with_catalogue: bool = False) -> ExampleList:
        """Every catalogued example with its tasks, and with their catalogue facts if asked."""
        root = self._catalog()
        try:
            return list_examples(root, with_task_details=with_catalogue)
        except CatalogError as exc:
            raise ProjectLookupError(
                HelperErrorCode.EXAMPLES_UNAVAILABLE, str(root), str(exc)
            ) from exc

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into its own new Git repository at ``destination``."""
        root = self._catalog()
        try:
            load_registry(root)
        except CatalogError as exc:
            raise ProjectLookupError(
                HelperErrorCode.EXAMPLES_UNAVAILABLE, str(root), str(exc)
            ) from exc
        try:
            project = new_example(root, example, destination, self.materializer)
        except CatalogError as exc:
            raise ProjectLookupError(HelperErrorCode.UNKNOWN_EXAMPLE, str(root), str(exc)) from exc
        except (DestinationError, ExampleMaterializationError) as exc:
            raise _lookup_error(exc, destination) from exc
        return CreatedProject(project=str(project.destination), tasks=project.tasks)

    def destination(self, example: str, destination: Path) -> tuple[ExampleDestination, str | None]:
        """What :meth:`create` would find at ``destination``, and the project there if it is one."""
        root = self._catalog()
        try:
            state, project = example_destination(root, example, destination)
        except CatalogError as exc:
            raise ProjectLookupError(HelperErrorCode.UNKNOWN_EXAMPLE, str(root), str(exc)) from exc
        return state, None if project is None else str(project)

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source``'s tracked files into their own new Git repository at ``destination``."""
        try:
            project = copy_out(source, destination, self.materializer)
        except FileNotFoundError as exc:
            raise ProjectLookupError(HelperErrorCode.PATH_MISSING, str(source), str(exc)) from exc
        except NotNestedError as exc:
            raise ProjectLookupError(HelperErrorCode.NOT_NESTED, str(source), str(exc)) from exc
        except (DestinationError, ExampleMaterializationError) as exc:
            raise _lookup_error(exc, destination) from exc
        return CreatedProject(project=str(project.destination), tasks=project.tasks)

    def _catalog(self) -> Path:
        if self.checkout is None:
            message = "this VibeSys is an installed distribution, which has no example catalog"
            raise ProjectLookupError(HelperErrorCode.EXAMPLES_UNAVAILABLE, "", message)
        return self.checkout


def _lookup_error(
    error: DestinationError | ExampleMaterializationError, destination: Path
) -> ProjectLookupError:
    """The typed helper error for a refused destination or a failed copy, naming the destination."""
    match error:
        case DestinationExistsError():
            code = HelperErrorCode.DESTINATION_EXISTS
        case DestinationError():
            code = HelperErrorCode.DESTINATION_NESTED
        case ExampleMaterializationError():
            code = HelperErrorCode.PROJECT_CREATE_FAILED
    return ProjectLookupError(code, str(destination), str(error))


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
