#!/usr/bin/env python3
"""Facts shared by the client setup action and its quality test.

Electron's postinstall downloads a binary the pnpm store does not contain.
``electron_version`` reads the one resolved version from a pnpm lockfile so
the cache key can name it.

``playwright install --with-deps`` runs ``apt-get update`` against every
configured source. Hosted runners ship a ``packages.microsoft.com`` list that
403s and fails its signature check. ``unguarded_playwright_with_deps`` reports
a command whose earlier script text does not remove that list first.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_ELECTRON_PACKAGE = re.compile(r"(?m)^  electron@(\d+\.\d+\.\d+(?:[-+][^\s:]+)?):\s*$")
_REMOVE_SOURCE = re.compile(r"(?m)^[ \t]*(?:sudo[ \t]+)?rm[ \t]")
PLAYWRIGHT_WITH_DEPS = "playwright install --with-deps"
MICROSOFT_APT_SOURCE = "packages.microsoft.com"
EXIT_OK = 0
EXIT_LOCKFILE = 1


class ElectronVersionError(ValueError):
    """The lockfile does not pin exactly one Electron version."""

    @classmethod
    def wrong_pin_count(cls, found: set[str]) -> ElectronVersionError:
        """Build the error listing the versions the lockfile pinned.

        Args:
            found: The distinct ``electron@`` versions in the lockfile.

        Returns:
            The error naming those versions, or ``none`` when the set is empty.
        """
        listed = ", ".join(sorted(found)) if found else "none"
        return cls(f"pnpm-lock.yaml must pin exactly one electron version, found {listed}")


def electron_version(lockfile: str) -> str:
    """Return the single ``electron@<version>`` pin in a pnpm lockfile.

    Args:
        lockfile: The lockfile text.

    Returns:
        The resolved Electron version.

    Raises:
        ElectronVersionError: The lockfile pins zero versions or more than one.
    """
    found = set(_ELECTRON_PACKAGE.findall(lockfile))
    if len(found) != 1:
        raise ElectronVersionError.wrong_pin_count(found)
    return next(iter(found))


def unguarded_playwright_with_deps(owner: str, steps: Sequence[object]) -> list[str]:
    """Return installs that would refresh the Microsoft apt source.

    Args:
        owner: Job or action name used in each message.
        steps: Ordered GitHub Actions steps. Non-mapping steps are ignored.

    Returns:
        One message per unguarded ``playwright install --with-deps``.
    """
    earlier: list[str] = []
    violations: list[str] = []
    for index, step in enumerate(steps, start=1):
        run = _run_script(step)
        if run is None:
            continue
        violations.extend(_unguarded_in_script(owner, index, _step_name(step), run, earlier))
        earlier.append(run)
    return violations


def violations_in_document(source: str, document: object) -> list[str]:
    """Return apt-guard violations for every job or composite action.

    Args:
        source: Repo-relative path of the workflow or action, for messages.
        document: Parsed YAML. Anything without jobs or composite steps is clean.

    Returns:
        The violations from each step list in the document.
    """
    found: list[str] = []
    for owner, steps in _step_groups(source, document):
        found.extend(unguarded_playwright_with_deps(owner, steps))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Print the Electron version pinned by a pnpm lockfile path.

    Args:
        argv: Arguments after the program name. ``None`` reads the process args.

    Returns:
        ``EXIT_OK`` when one version is printed, otherwise ``EXIT_LOCKFILE``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lockfile")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        text = _read_lockfile(args.lockfile)
        version = electron_version(text)
    except (ElectronVersionError, OSError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_LOCKFILE
    print(version)
    return EXIT_OK


def _read_lockfile(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        message = f"cannot read {path}: {exc.strerror or exc}"
        raise OSError(message) from exc


def _step_groups(source: str, document: object) -> list[tuple[str, list[object]]]:
    if not isinstance(document, dict):
        return []
    jobs = document.get("jobs")
    if isinstance(jobs, dict):
        return [_named_steps(f"{source} job {name}", job) for name, job in jobs.items()]
    runs = document.get("runs")
    if isinstance(runs, dict) and runs.get("using") == "composite":
        return [_named_steps(source, runs)]
    return []


def _named_steps(owner: str, node: object) -> tuple[str, list[object]]:
    steps = node.get("steps") if isinstance(node, dict) else None
    return owner, list(steps) if isinstance(steps, list) else []


def _run_script(step: object) -> str | None:
    if not isinstance(step, dict):
        return None
    run = step.get("run")
    if not isinstance(run, str):
        return None
    return run


def _step_name(step: object) -> str:
    if not isinstance(step, dict):
        return ""
    name = step.get("name")
    return name if isinstance(name, str) else ""


def _unguarded_in_script(
    owner: str,
    index: int,
    name: str,
    run: str,
    earlier: Sequence[str],
) -> list[str]:
    violations: list[str] = []
    start = 0
    while True:
        at = run.find(PLAYWRIGHT_WITH_DEPS, start)
        if at < 0:
            return violations
        prior = "\n".join([*earlier, run[:at]])
        if not _microsoft_apt_source_removed(prior):
            label = f"{owner} step {index}"
            if name:
                label = f"{label} ({name})"
            violations.append(
                f"{label} runs `{PLAYWRIGHT_WITH_DEPS}` before removing the "
                f"{MICROSOFT_APT_SOURCE} apt source. Delete that source list "
                "before the install so apt-get update does not refresh it."
            )
        start = at + len(PLAYWRIGHT_WITH_DEPS)


def _microsoft_apt_source_removed(prior: str) -> bool:
    host = prior.find(MICROSOFT_APT_SOURCE)
    if host < 0:
        return False
    return _REMOVE_SOURCE.search(prior, host) is not None


if __name__ == "__main__":
    raise SystemExit(main())
