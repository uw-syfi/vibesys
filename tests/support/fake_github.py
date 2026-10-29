"""A GitHub CLI for tests that decide whether ``gh`` has an account: signed in, or not.

Both answer ``gh auth status`` and ``gh api user`` as the real CLI does, through scripted
processes, so no test starts a real ``gh``. ``asked`` says whether the code under test ran ``gh``
at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_github.api import GitHubCLI
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript

if TYPE_CHECKING:
    from vs_sim.api import ProcessSpec

NOT_LOGGED_IN = "You are not logged into any GitHub hosts."
"""What ``gh auth status`` says on a machine with no account."""


@dataclass(frozen=True)
class FakeGitHub:
    """A GitHub CLI over scripted processes, and the record of what it ran."""

    cli: GitHubCLI
    processes: FakeProcesses

    @property
    def asked(self) -> bool:
        """Whether any ``gh`` command ran."""
        return bool(self.processes.ran)


def _fake(script: FakeProcesses) -> FakeGitHub:
    return FakeGitHub(cli=GitHubCLI(script), processes=script)


def signed_in_github(login: str = "octocat") -> FakeGitHub:
    """A GitHub CLI whose account is ``login``."""

    def script(spec: ProcessSpec) -> ProcessScript:
        if tuple(spec.argv[:3]) == ("gh", "api", "user"):
            return ProcessScript(returncode=0, stdout=f"{login}\n".encode())
        return ProcessScript(returncode=0)

    return _fake(FakeProcesses(script, pass_time=ManualClock().advance))


def signed_out_github(reason: str = NOT_LOGGED_IN) -> FakeGitHub:
    """A GitHub CLI with no account; ``gh auth status`` says ``reason``."""

    def script(_spec: ProcessSpec) -> ProcessScript:
        return ProcessScript(returncode=1, stderr=reason.encode())

    return _fake(FakeProcesses(script, pass_time=ManualClock().advance))
