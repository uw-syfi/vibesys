"""Fixtures shared by the tests under ``tests/``.

Nothing here imports the product. pytest imports this file in the xdist controller
as well as in every worker, and the controller runs no test: a top-level ``vibesys``
import made it build the whole model graph (seconds, several times that under
coverage) for nothing. A fixture that needs the product lives in the test module that
uses it.
"""

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from filelock import FileLock
from tests.support.distribution_build import (
    BuiltDistributions,
    BuiltWheel,
    build_distribution,
    copy_private_packaging_source,
    reissue_private_packaging_source,
)


@pytest.fixture
def isolated_github_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make GitHub authentication independent of developer credentials."""
    home = tmp_path / "home"
    github_config = tmp_path / "gh-config"
    home.mkdir()
    github_config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GH_CONFIG_DIR", str(github_config))
    for variable in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    ):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A directory shallow enough to hold a bindable Unix socket.

    ``tmp_path`` is not usable for this. It nests a per-user, per-session and
    per-test directory under the platform temp root, and on macOS that root is
    already a ~50-byte ``/var/folders/...`` path, so the result overruns
    ``sockaddr_un.sun_path`` (104 bytes there, 108 on Linux) before a socket
    name is even appended and ``bind`` fails with "AF_UNIX path too long".

    Rooting the directory at ``/tmp`` keeps every path it yields far under the
    limit on both platforms.

    The path is resolved: ``/tmp`` is a symlink to ``/private/tmp`` on macOS, and a server
    resolves the socket path it is given, so a listener bound at the resolved address is not
    found by a client that dials the unresolved one.
    """
    directory = Path(tempfile.mkdtemp(prefix="vibesys-sock-", dir="/tmp")).resolve()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parents[1]


@pytest.fixture(scope="session")
def built_distributions(
    tmp_path_factory: pytest.TempPathFactory, repo_root: Path
) -> BuiltDistributions:
    """One sdist and one wheel of the current checkout, built once per run in private scratch.

    A build takes tens of seconds of CPU, and every packaging check only inspects the result,
    so they all share this one. Under xdist the workers share it too: the first to ask builds
    it while holding a file lock in the run's common temp directory, and the others reuse it,
    whichever ``--dist`` mode scheduled their tests.
    """
    base = tmp_path_factory.getbasetemp()
    scratch = (base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base) / "built"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    dist = scratch / "dist"
    with FileLock(str(scratch.parent / "built.lock")):
        if not dist.is_dir():
            staging = scratch.parent / "built-staging"
            staging.mkdir()
            source = copy_private_packaging_source(repo_root, staging / "checkout")
            build_distribution(source, staging / "dist", artifact="all")
            staging.rename(scratch)
    # The snapshot's capability is process-local, so each worker re-issues it over the
    # finished copy instead of sharing the first builder's object.
    source = reissue_private_packaging_source(scratch / "checkout")
    return BuiltDistributions(
        source=source,
        sdist=next(dist.glob("vibesys-*.tar.gz")),
        wheel=next(dist.glob("vibesys-*.whl")),
    )


@pytest.fixture(scope="session")
def built_wheel(built_distributions: BuiltDistributions) -> BuiltWheel:
    """The shared build's wheel, with the snapshot it came from."""
    return BuiltWheel(source=built_distributions.source, wheel=built_distributions.wheel)
