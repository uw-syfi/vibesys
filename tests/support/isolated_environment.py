"""Isolate default operator state before pytest imports project modules.

Each pytest process owns its home and runtime directory. Children inherit the
same defaults, while xdist workers create independent directories when loading
this plugin. Explicit environment inputs inside a test remain supported.
"""

from __future__ import annotations

import os
import tempfile
from hashlib import sha256
from pathlib import Path

import pytest
from tests.support.posix_tools import install_posix_tool_shims

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "VibeSys Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "VibeSys Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
"""The author and committer every test-started Git commit gets."""

HERMETIC_GIT_VARIABLES = (
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_TERMINAL_PROMPT",
    *GIT_IDENTITY,
)
"""The variables that make test Git ignore the operator's config; no test may reset them."""


def hermetic_git_environment(directory: Path) -> dict[str, str]:
    """Create an empty global Git config in ``directory`` and return the variables that select it."""
    empty_config = directory / "empty-gitconfig"
    empty_config.write_text("", encoding="utf-8")
    return {
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        **GIT_IDENTITY,
    }


class _SuiteEnvironment:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="vs-pytest-", dir="/tmp")
        self.environment = pytest.MonkeyPatch()
        root = Path(self.directory.name)
        original_home = Path.home()
        # Installed toolchains are inputs, independent of operator state. Keep
        # rustup's installed toolchain visible when HOME is made private.
        for variable, name in (("CARGO_HOME", ".cargo"), ("RUSTUP_HOME", ".rustup")):
            existing = original_home / name
            if variable not in os.environ and existing.is_dir():
                self.environment.setenv(variable, str(existing))
        # Go's content-addressed compilation/module caches support concurrent
        # processes. Share them among workers through inherited explicit paths,
        # unlike mutable VibeSys state and runtime locks, which stay private.
        for variable, name in (("GOCACHE", "go-build"), ("GOMODCACHE", "go-modules")):
            if variable not in os.environ:
                self.environment.setenv(variable, str(root / name))
        # Scripts the executing Fake Slurm cluster runs locally must stay
        # portable to macOS, so every test sees the strict tools first.
        shims = install_posix_tool_shims(root / "posix-bin")
        self.environment.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
        # A Git commit starts auto maintenance, detached from Git 2.47 on, and
        # that maintenance prunes any worktree whose admin directory has no
        # gitdir file yet. A test's next ``git worktree add`` is in exactly that
        # state between creating ``.git/worktrees/<name>`` and locking it, so the
        # background prune can delete it mid-command. ``Git`` already
        # disables maintenance for product Git; this covers every Git a test
        # starts itself. Command-line config survives tests that replace HOME.
        for index, (key, value) in enumerate((("maintenance.auto", "false"), ("gc.auto", "0"))):
            self.environment.setenv(f"GIT_CONFIG_KEY_{index}", key)
            self.environment.setenv(f"GIT_CONFIG_VALUE_{index}", value)
        self.environment.setenv("GIT_CONFIG_COUNT", "2")
        # The developer's global and system Git config (failing hooks, forced commit
        # signing, another default branch) must not change a test's result. Point Git at
        # an empty global file, ignore the system file, and fix the identity so no test
        # depends on ``user.name`` being configured. The variables are inherited by every
        # child, and survive tests that replace HOME.
        for variable, value in hermetic_git_environment(root).items():
            self.environment.setenv(variable, value)
        for variable, name in (
            ("HOME", "home"),
            ("VIBESYS_STATE_HOME", "state"),
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_STATE_HOME", "xdg-state"),
            ("XDG_RUNTIME_DIR", "runtime"),
        ):
            path = root / name
            path.mkdir(mode=0o700)
            self.environment.setenv(variable, str(path))

    def pytest_configure(self, config: pytest.Config) -> None:
        config.add_cleanup(self.close)

    def close(self) -> None:
        self.environment.undo()
        self.directory.cleanup()


def pytest_addoption(pluginmanager: pytest.PytestPluginManager) -> None:
    """Initialize at registration, before descendant conftests and collection.

    ``pytest_configure`` is too late: descendant conftests can import modules
    that capture runtime paths at import time. Plugin registration happens as
    the root conftest loads, before pytest descends into tests or libraries.
    """
    pluginmanager.register(_SuiteEnvironment(), "suite-private-environment")


@pytest.fixture(autouse=True)
def isolated_vibesys_state_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> None:
    """Keep machine-local project state separate for each test, across all roots."""
    identity = sha256(request.node.nodeid.encode()).hexdigest()
    # Pure tests need no filesystem allocation. State stores create their root
    # only when exercised; getbasetemp creates one directory per worker session.
    state_home = tmp_path_factory.getbasetemp() / f".vibesys-state-{identity}"
    monkeypatch.setenv(
        "VIBESYS_STATE_HOME",
        str(state_home),
    )
