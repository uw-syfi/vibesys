"""Which run environments hand their agent the compute backend's accelerator on this host.

A run in such an environment checks the accelerator before its image build, and
``vibesys preflight`` asks the same question through ``attaches_backend_accelerator``.
"""

from __future__ import annotations

import pytest

from vs_runtime.api.infrastructure import RunEnvironmentSpec, build_run_environment


@pytest.mark.parametrize("docker_in_docker", [False, True])
def test_the_docker_editor_holds_the_accelerator_unless_it_runs_docker_in_docker(
    *, docker_in_docker: bool
) -> None:
    environment = build_run_environment(RunEnvironmentSpec("docker"))

    assert environment.attaches_backend_accelerator(docker_in_docker=docker_in_docker) is (
        not docker_in_docker
    )


@pytest.mark.parametrize(
    "spec",
    [RunEnvironmentSpec("modal", {"gpu": "A10G"}), RunEnvironmentSpec("host")],
    ids=["modal", "host"],
)
def test_an_environment_whose_accelerator_is_elsewhere_attaches_none_here(
    spec: RunEnvironmentSpec,
) -> None:
    environment = build_run_environment(spec)

    assert environment.attaches_backend_accelerator(docker_in_docker=False) is False


@pytest.mark.parametrize("docker_in_docker", [False, True])
def test_only_the_docker_editor_needs_sysbox_and_only_for_docker_in_docker(
    *, docker_in_docker: bool
) -> None:
    docker = build_run_environment(RunEnvironmentSpec("docker"))

    assert docker.requires_sysbox_runtime(docker_in_docker=docker_in_docker) is docker_in_docker


@pytest.mark.parametrize(
    "spec",
    [RunEnvironmentSpec("modal", {"gpu": "A10G"}), RunEnvironmentSpec("host")],
    ids=["modal", "host"],
)
def test_an_environment_that_refuses_docker_in_docker_needs_no_sysbox(
    spec: RunEnvironmentSpec,
) -> None:
    environment = build_run_environment(spec)

    assert environment.requires_sysbox_runtime(docker_in_docker=True) is False
