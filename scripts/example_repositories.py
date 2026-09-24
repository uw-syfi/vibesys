"""Discovery and fetching of the repository-native example submodules.

A repository-native example is an external repository (a candidate repository
that lives outside this one) that carries its VibeSys tasks in a ``.vibesys/``
directory (``docs/running-vibesys.md``). This repository tracks each one as a
submodule under ``examples/<family>/repositories/<name>``.

Validating those tasks only needs ``.vibesys/``, not the candidate source, so
this module fetches ``.vibesys/`` alone: a blob-filtered, depth-1 fetch of the
pinned gitlink commit with a sparse checkout. On the DeathStarBench example
that is ~3MB and ~2s, against ~95s and hundreds of MB for a full submodule
checkout.
"""

from __future__ import annotations

import argparse
import configparser
import errno
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The one directory a repository-native example must contribute.
VIBESYS_DIRNAME = ".vibesys"

#: ``examples/<family>/repositories/<name>``: the path shape that marks a
#: submodule as a runnable candidate repository rather than a reference one.
_EXAMPLE_PATH_PARTS = 4

#: Git's mode for a gitlink entry, as reported by ``git ls-tree``.
_GITLINK_MODE = "160000"


class ExampleRepositoryError(RuntimeError):
    """Raised when a submodule declaration or its ``.vibesys/`` directory cannot be resolved."""

    @classmethod
    def command_failed(cls, command: str, cwd: Path, stderr: str) -> ExampleRepositoryError:
        """Report a git invocation that exited nonzero, with git's own message."""
        return cls(f"`{command}` failed in {cwd}: {stderr}")

    @classmethod
    def untracked(cls, path: Path) -> ExampleRepositoryError:
        """Report a ``.gitmodules`` path that HEAD does not track."""
        return cls(f"{path} is not tracked in HEAD")

    @classmethod
    def not_a_gitlink(cls, path: Path, mode: str) -> ExampleRepositoryError:
        """Report a tracked path that is an ordinary tree rather than a submodule."""
        return cls(f"{path} is not a submodule gitlink (mode {mode})")

    @classmethod
    def missing_vibesys_dir(cls, path: Path, commit: str) -> ExampleRepositoryError:
        """Report an example whose pinned commit carries no task directory."""
        return cls(f"{path} at {commit} has no {VIBESYS_DIRNAME}/ directory")


@dataclass(frozen=True)
class ExampleRepository:
    """One ``examples/<family>/repositories/<name>`` submodule.

    ``path`` is relative to the repository root; ``commit`` is the gitlink SHA
    the superproject pins, which is what CI must validate rather than whatever
    the tracked branch happens to point at today.
    """

    path: Path
    url: str
    branch: str
    commit: str

    @property
    def name(self) -> str:
        """Return the example's directory name, which is how docs refer to it."""
        return self.path.name


def is_example_repository_path(path: Path) -> bool:
    """Report whether ``path`` has the repository-native example shape.

    The layout ``examples/<family>/repositories/<name>`` is what distinguishes a
    runnable candidate repository from the reference and tooling submodules,
    which are opt-in (``update = none``) and carry no tasks.
    """
    parts = path.parts
    return (
        len(parts) == _EXAMPLE_PATH_PARTS and parts[0] == "examples" and parts[2] == "repositories"
    )


def _git(*args: str, cwd: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError(errno.ENOENT, "git executable was not found on PATH", "git")
    # lint-waiver: LW-008045 [S603]; The resolved Git executable receives repo-configured path arguments without a shell.
    result = subprocess.run(  # noqa: S603
        [git, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExampleRepositoryError.command_failed(
            " ".join((git, *args)), cwd, result.stderr.strip()
        )
    return result.stdout


def _pinned_commit(repo_root: Path, path: Path) -> str:
    entry = _git("ls-tree", "HEAD", "--", str(path), cwd=repo_root).strip()
    if not entry:
        raise ExampleRepositoryError.untracked(path)
    mode, object_type, rest = entry.split(" ", 2)
    if mode != _GITLINK_MODE or object_type != "commit":
        raise ExampleRepositoryError.not_a_gitlink(path, mode)
    return rest.split("\t", 1)[0]


def discover_example_repositories(repo_root: Path = REPO_ROOT) -> tuple[ExampleRepository, ...]:
    """Read ``.gitmodules`` and return every repository-native example it declares.

    Data-driven on purpose: adding an example repository is a ``.gitmodules``
    change, and neither CI nor this module should need editing for it.
    """
    config = configparser.ConfigParser()
    config.read(repo_root / ".gitmodules")

    repositories: list[ExampleRepository] = []
    for section in config.sections():
        if not section.startswith('submodule "'):
            continue
        path = Path(config.get(section, "path"))
        if not is_example_repository_path(path):
            continue
        repositories.append(
            ExampleRepository(
                path=path,
                url=config.get(section, "url"),
                branch=config.get(section, "branch"),
                commit=_pinned_commit(repo_root, path),
            )
        )
    return tuple(sorted(repositories, key=lambda repository: repository.path))


def _manifest_paths(vibesys_dir: Path) -> Iterator[Path]:
    yield from sorted(vibesys_dir.glob("tasks/*/vibesys.input.toml"))


def _declared_project_paths(manifest: Path) -> Iterator[str]:
    """Yield the project-root-relative paths a manifest points at outside ``.vibesys/``.

    Two manifest fields can name a file in the candidate repository rather than
    in the task directory, and ``vibesys validate`` requires both to exist:
    ``[environment.modal] entrypoint``, and an ``accuracy``/``benchmark``
    ``command`` whose executable is a repository path rather than a bare
    program name.

    Parsed with ``tomllib`` rather than through ``vibesys.evaluators.input_manifest`` on
    purpose: this runs *before* validation, so it has to tolerate a manifest
    that validation is about to reject.
    """
    try:
        document = tomllib.loads(manifest.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return

    modal = document.get("environment", {}).get("modal", {})
    entrypoint = modal.get("entrypoint")
    if isinstance(entrypoint, str):
        yield entrypoint

    for section in ("accuracy", "benchmark"):
        command = document.get(section, {}).get("command")
        if isinstance(command, list) and command and isinstance(command[0], str):
            executable = command[0]
            if "/" in executable:
                yield executable


def _sparse_directories(vibesys_dir: Path) -> tuple[str, ...]:
    """Return the extra cone-mode directories the ``.vibesys/`` manifests depend on."""
    directories: set[str] = set()
    for manifest in _manifest_paths(vibesys_dir):
        for declared in _declared_project_paths(manifest):
            candidate = Path(declared)
            if candidate.is_absolute() or ".." in candidate.parts:
                # `vibesys validate` rejects these itself, with a better message.
                continue
            parent = candidate.parent
            if parent != Path():
                directories.add(parent.as_posix())
    return tuple(sorted(directories))


def fetch_external_repo(repository: ExampleRepository, repo_root: Path = REPO_ROOT) -> Path:
    """Materialize ``<repository>/.vibesys`` at the pinned commit, and little else.

    Idempotent: a checkout already sitting at the pinned commit is left alone.
    The resulting directory is a real git repository whose ``HEAD`` matches the
    gitlink, so the superproject sees the submodule as checked out and clean.

    A second sparse-checkout pass widens the cone to the directories the
    ``.vibesys/``'s own manifests reference (see ``_declared_project_paths``), so a
    task that names a deployment entrypoint in the candidate repository still
    validates without cloning the repository.
    """
    target = repo_root / repository.path
    target.mkdir(parents=True, exist_ok=True)

    if not (target / ".git").exists() or _git("rev-parse", "HEAD", cwd=target).strip() != (
        repository.commit
    ):
        _git("init", "--quiet", cwd=target)
        remotes = _git("remote", cwd=target).split()
        if "origin" in remotes:
            _git("remote", "set-url", "origin", repository.url, cwd=target)
        else:
            _git("remote", "add", "origin", repository.url, cwd=target)

        # Cone mode also materializes the root-level files, which is both cheap
        # and what a reader expects from a checkout; nothing else comes down.
        _git("sparse-checkout", "init", "--cone", cwd=target)
        _git("sparse-checkout", "set", VIBESYS_DIRNAME, cwd=target)
        _git("fetch", "--depth", "1", "--filter=blob:none", "origin", repository.commit, cwd=target)
        _git("checkout", "--detach", repository.commit, cwd=target)

    vibesys_dir = target / VIBESYS_DIRNAME
    if not vibesys_dir.is_dir():
        raise ExampleRepositoryError.missing_vibesys_dir(repository.path, repository.commit)

    extra = _sparse_directories(vibesys_dir)
    if extra:
        _git("sparse-checkout", "set", VIBESYS_DIRNAME, *extra, cwd=target)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch every declared example's external repository, reporting each one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to read .gitmodules and gitlinks from.",
    )
    arguments = parser.parse_args(argv)
    repo_root = arguments.repo_root.expanduser().resolve()

    repositories = discover_example_repositories(repo_root)
    if not repositories:
        print(f"No repository-native example submodules declared in {repo_root / '.gitmodules'}")
        return 1

    for repository in repositories:
        fetch_external_repo(repository, repo_root)
        print(f"{repository.path}: {VIBESYS_DIRNAME}/ at {repository.commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
