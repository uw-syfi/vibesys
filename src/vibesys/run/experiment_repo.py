"""Remote Git tracking for a complete experiment directory."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vibesys.repository import REPOSITORY_SLUG, RepositoryVisibility

_EXPERIMENT_GITIGNORE = """\
# Runtime logs are useful locally but are noisy and may contain provider output.
logs/*.log
logs/snapshots/
"""


@dataclass(frozen=True)
class ExperimentRepository:
    """Create and synchronize the Git repository containing one experiment."""

    root: Path
    log: Callable[[str], None]

    _GIT_IDENTITY = {
        "GIT_AUTHOR_NAME": "vibesys",
        "GIT_AUTHOR_EMAIL": "vibesys@local",
        "GIT_COMMITTER_NAME": "vibesys",
        "GIT_COMMITTER_EMAIL": "vibesys@local",
    }

    def create_remote(self, slug: str, visibility: RepositoryVisibility) -> None:
        """Create a GitHub repository and attach it as ``origin``."""
        if not REPOSITORY_SLUG.fullmatch(slug):
            raise ValueError(f"--repo must be a GitHub OWNER/NAME pair, got {slug!r}")
        if self.has_origin():
            raise ValueError(f"experiment repository already has an origin remote: {self.root}")

        self._ensure_gitignore()
        self._run(
            [
                "gh",
                "repo",
                "create",
                slug,
                f"--{visibility.value}",
                "--source",
                str(self.root),
                "--remote",
                "origin",
            ],
            tool="GitHub CLI",
        )
        self.log(f"[repo] created GitHub repository {slug}")

    def has_origin(self) -> bool:
        """Return whether the experiment repository has an ``origin`` remote."""
        result = self._run(
            ["git", "remote", "get-url", "origin"],
            check=False,
            tool="git",
        )
        return result.returncode == 0

    def sync(self) -> None:
        """Commit durable experiment state and push the current branch."""
        if not self.has_origin():
            return

        self._ensure_gitignore()
        self._run(["git", "add", "-A"], tool="git")
        changed = self._run(
            ["git", "diff", "--cached", "--quiet"],
            check=False,
            tool="git",
        ).returncode
        if changed:
            self._run(
                ["git", "commit", "-m", "chore: sync experiment state"],
                tool="git",
            )
        self._run(["git", "push", "-u", "origin", "HEAD"], tool="git")
        self.log("[repo] pushed experiment state to origin")

    def _ensure_gitignore(self) -> None:
        path = self.root / ".gitignore"
        existing = path.read_text() if path.is_file() else ""
        if "logs/*.log" in existing and "logs/snapshots/" in existing:
            return
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + _EXPERIMENT_GITIGNORE)

    def _run(
        self,
        command: list[str],
        *,
        check: bool = True,
        tool: str,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, **self._GIT_IDENTITY}
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{tool} is required for experiment repository tracking") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"{tool} command failed ({' '.join(command)}): {detail}")
        return result
