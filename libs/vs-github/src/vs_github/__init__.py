"""Public API for the ``vs-github`` library."""

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
