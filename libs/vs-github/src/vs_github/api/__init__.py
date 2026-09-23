"""Public adapter for the GitHub CLI.

``GitHubCLI`` runs GitHub operations through ``gh``. Failures are reported as
``GitHubCLIError`` or its unavailable and authentication subclasses.
"""

from vs_github.cli import (
    GitHubAuthenticationError,
    GitHubCLI,
    GitHubCLIError,
    GitHubCLIUnavailableError,
)

__all__ = [
    "GitHubAuthenticationError",
    "GitHubCLI",
    "GitHubCLIError",
    "GitHubCLIUnavailableError",
]
