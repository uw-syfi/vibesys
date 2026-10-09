"""Which ``GitRepository`` implementation a project runs on is decided in one place."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_project.api import (
    DEFAULT_GIT_BACKEND,
    GIT_BACKEND_ENV,
    CliGitRepository,
    GitBackend,
    GitBackendError,
    NullGitTrackerEvents,
    Pygit2GitRepository,
    open_git_repository,
    select_git_backend,
)

if TYPE_CHECKING:
    from pathlib import Path


@given(available=st.booleans())
def test_unset_selects_the_default_when_installed_and_the_cli_otherwise(*, available: bool) -> None:
    expected = DEFAULT_GIT_BACKEND if available else GitBackend.CLI

    assert select_git_backend(None, pygit2_available=available) is expected
    assert select_git_backend("", pygit2_available=available) is expected


@given(available=st.booleans())
def test_the_cli_is_always_selectable(*, available: bool) -> None:
    assert select_git_backend("cli", pygit2_available=available) is GitBackend.CLI


def test_pygit2_is_selectable_only_when_installed() -> None:
    assert select_git_backend("pygit2", pygit2_available=True) is GitBackend.PYGIT2
    with pytest.raises(GitBackendError, match=GIT_BACKEND_ENV):
        select_git_backend("pygit2", pygit2_available=False)


@given(name=st.text(max_size=12).filter(lambda text: text and text not in {"cli", "pygit2"}))
def test_an_unknown_name_is_an_error_that_names_the_variable_and_the_value(name: str) -> None:
    with pytest.raises(GitBackendError) as raised:
        select_git_backend(name, pygit2_available=True)

    assert GIT_BACKEND_ENV in str(raised.value)
    assert repr(name) in str(raised.value)


def test_the_configured_backend_builds_its_implementation(tmp_path: Path) -> None:
    faults = NullGitTrackerEvents()

    cli = open_git_repository(tmp_path, faults=faults, environ={GIT_BACKEND_ENV: "cli"})
    pygit2 = open_git_repository(tmp_path, faults=faults, environ={GIT_BACKEND_ENV: "pygit2"})

    assert isinstance(cli, CliGitRepository)
    assert isinstance(pygit2, Pygit2GitRepository)


def test_a_bad_configuration_fails_when_the_repository_is_opened(tmp_path: Path) -> None:
    with pytest.raises(GitBackendError):
        open_git_repository(
            tmp_path, faults=NullGitTrackerEvents(), environ={GIT_BACKEND_ENV: "svn"}
        )
