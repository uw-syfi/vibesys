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
