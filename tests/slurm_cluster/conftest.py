"""Fixtures for the ``slurm_cluster`` tier: one real Slurm cluster per test session.

The tier is opt-in (``VIBESYS_SLURM_CLUSTER=1``) and needs Docker (the compute node runs
privileged). Run it with ``scripts/run_slurm_cluster_tests.sh``.
"""

from __future__ import annotations

import os
import pwd
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.cluster import (
    SlurmCluster,
    build_image,
    containers_labelled,
    docker_problem,
    start_cluster,
)
from tests.slurm_cluster.harness import OpenRun, build_agent_image, open_run

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

ENABLE_ENV = "VIBESYS_SLURM_CLUSTER"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip the tier unless it is enabled, and keep it on one xdist worker.

    The tier shares one cluster, so its tests must not be spread over workers
    (each would start its own cluster).
    """
    enabled = os.environ.get(ENABLE_ENV) == "1"
    problem = docker_problem() if enabled else None
    for item in items:
        if item.get_closest_marker("slurm_cluster") is None:
            continue
        item.add_marker(pytest.mark.xdist_group("slurm_cluster"))
        item.add_marker(pytest.mark.serial)
        if not enabled:
            item.add_marker(
                pytest.mark.skip(reason=f"set {ENABLE_ENV}=1 to run the real-cluster tier")
            )
        elif problem is not None:
            item.add_marker(pytest.mark.skip(reason=problem))


@pytest.fixture(scope="session")
def agent_image_id() -> str:
    """The agent container image (built once; Docker's cache makes a rebuild a no-op)."""
    return build_agent_image()


@pytest.fixture(scope="session")
def slurm_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SlurmCluster]:
    """Start the cluster once for the session and remove it at the end.

    Its shared directory lives under the pytest base temp directory, which must
    be on a local filesystem Docker can bind-mount.
    """
    user = pwd.getpwuid(os.getuid()).pw_name
    image = build_image(
        dockerfile="Dockerfile",
        tag_prefix="vibesys-slurm-test-cluster",
        build_args={"USER_NAME": user, "USER_UID": str(os.getuid()), "USER_GID": str(os.getgid())},
    )
    root = tmp_path_factory.mktemp("cluster-fs")
    cluster = start_cluster(root, image=image)
    try:
        yield cluster
    finally:
        cluster.stop()
        assert not list(containers_labelled(cluster.cluster_id)), "cluster containers leaked"


#: A host variable standing in for a credential; no job or gate may ever see it.
HOST_CANARY_ENV = "VIBESYS_TEST_HOST_CANARY"
HOST_CANARY_VALUE = "never-in-a-job-0f3a9c"


class SharedRuns:
    """Opened run environments, one per kind, shared by the tests that do not close them.

    Opening a Docker editor starts a container and its brokers, so tests that only
    use a session share one; tests about closing open their own.
    """

    def __init__(self, cluster: SlurmCluster, directory: Path, image_id: str) -> None:
        self._cluster = cluster
        self._directory = directory
        self._image_id = image_id
        self._opened: list[AbstractContextManager[OpenRun]] = []
        self._runs: dict[str, OpenRun] = {}

    def get(self, kind: str) -> OpenRun:
        """Return the open run of *kind*, opening it on first use."""
        if kind not in self._runs:
            manager = open_run(kind, self._cluster, self._directory, self._image_id)
            self._runs[kind] = manager.__enter__()
            self._opened.append(manager)
        return self._runs[kind]

    def reset_modes(self) -> None:
        """Reset the steered gate modes of every opened run (see ``OpenRun.reset_modes``)."""
        for run in self._runs.values():
            run.reset_modes()

    def close(self) -> None:
        """Close every opened run, all at once so their slow container stops overlap."""
        with ThreadPoolExecutor() as pool:
            for outcome in [pool.submit(m.__exit__, None, None, None) for m in self._opened]:
                outcome.result()


@pytest.fixture(scope="session")
def shared_runs(
    slurm_cluster: SlurmCluster, agent_image_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[SharedRuns]:
    """The registry of shared runs; the host holds a secret while they are open."""
    patch = pytest.MonkeyPatch()
    patch.setenv(HOST_CANARY_ENV, HOST_CANARY_VALUE)
    runs = SharedRuns(slurm_cluster, tmp_path_factory.mktemp("configs"), agent_image_id)
    try:
        yield runs
    finally:
        runs.close()
        patch.undo()


@pytest.fixture(scope="session", params=["slurm", "slurm-gpu"])
def run(request: pytest.FixtureRequest, shared_runs: SharedRuns) -> OpenRun:
    """A shared open run of each environment kind in turn."""
    return shared_runs.get(request.param)


@pytest.fixture(scope="session")
def gpu_run(shared_runs: SharedRuns) -> OpenRun:
    """The shared open ``slurm-gpu`` run."""
    return shared_runs.get("slurm-gpu")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A per-test scratch directory for configuration files (not shared with the cluster)."""
    return tmp_path


@pytest.fixture(autouse=True)
def _no_jobs_survive_a_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Cancel whatever a test left in the queue, so one failure cannot poison the next."""
    yield
    if (
        request.node.get_closest_marker("slurm_cluster") is not None
        and "slurm_cluster" in request.fixturenames
    ):
        cluster: SlurmCluster = request.getfixturevalue("slurm_cluster")
        cluster.cancel_all()


@pytest.fixture(autouse=True)
def _no_steered_mode_survives_a_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Unset the gate modes a test steered, so a shared run starts every test on ``pass``."""
    yield
    if "shared_runs" in request.fixturenames:
        request.getfixturevalue("shared_runs").reset_modes()


def expect_failure_for(request: pytest.FixtureRequest, run: OpenRun, kind: str, issue: int) -> None:
    """Mark the running test as a strict known failure for *kind* runs, tracked by *issue*.

    Strict: when the bug is fixed the test passes unexpectedly and fails the tier
    until this call is removed, so the marker cannot outlive the bug.
    """
    if run.session.view.env_kind == kind:
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=f"known bug: uw-syfi/vibesys#{issue}")
        )
