from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from tests.support import provider_profiles as fake_profiles

from vibesys.backends import SandboxKind
from vibesys.constants import ComputeBackend
from vibesys.domains.environment import EnvironmentBindMount
from vibesys.evaluators import (
    EvaluatorPackageRequirement,
    EvaluatorToolLifecycleHooks,
    evaluator_tools_install_command,
    resolve_evaluator_package,
    tool_install_root,
    tool_spec_digest,
)
from vibesys.input_manifest import load_project_task
from vibesys.profilers import ProfilerKind
from vibesys.sandbox.run_environment import (
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    _cli_container_env,
    _cli_provider_env_and_auth_files,
    _container_mount_plan,
    _docker_agent_toolchains,
    _docker_evaluator_tool_mounts,
    _evaluator_container_setup,
    _evaluator_tools,
    _EvaluatorToolBuildRequiredError,
    _resolve_docker_image_id,
    _SkyPilotRunEnvironmentSession,
    _symlink_lifecycle_hooks,
    build_run_environment,
    make_run_environment_spec,
    run_environment_record,
)
from vs_project import Project, RunEnvironmentRecord, RunResourceRequest
from vs_sandbox import (
    BeforeReadyContext,
    HostResource,
    HostResourceAccess,
    ProjectPathPolicy,
    SandboxLifecycle,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from deepagents.backends.protocol import SandboxBackendProtocol

    from vibesys.backends.base import ContentionMonitor


# A committed two-file overlay, not a submodule: the contract under test is
# how the run environment expands and quotes nested shell argv, not any
# particular candidate repository.
NESTED_SHELL_PROJECT = Path(__file__).parent / "fixtures" / "nested_shell_project"


def _as_mount_tuples(resources: Sequence[HostResource]) -> list[tuple[str, str, bool]]:
    """Invert ``_resource_for_mount`` for assertions written against the old shape.

    Lets tests keep comparing against plain ``(host, container, readonly)``
    tuples after ``_container_mount_plan`` moved from raw bind mounts to a
    ``HostResource`` list.
    """
    return [
        (
            str(resource.path),
            resource.agent_path if resource.agent_path is not None else str(resource.path),
            resource.access is HostResourceAccess.READ_ONLY,
        )
        for resource in resources
    ]


def _fake_agent_path(
    host_workspace: str, resources: Sequence[HostResource]
) -> Callable[[Path | str], str]:
    """Build a lookup mirroring ``DockerSandbox.agent_path`` from the same inputs.

    Longest host-prefix wins, exactly like the real sandbox, so a test can
    assert on ``AgentPaths`` without starting a real container.
    """
    entries = [
        (str(Path(host_workspace)), "/workspace"),
        *(
            (str(resource.path), resource.agent_path or str(resource.path))
            for resource in resources
        ),
    ]
    entries.sort(key=lambda pair: len(pair[0]), reverse=True)

    def lookup(host_path: Path | str) -> str:
        normalized = str(Path(host_path))
        for host_prefix, container_prefix in entries:
            if normalized == host_prefix:
                return container_prefix
            if normalized.startswith(host_prefix + "/"):
                return container_prefix + normalized[len(host_prefix) :]
        return normalized

    return lookup


class FakeBackend:
    """Structural stand-in for ``ComputeBackendImpl`` that records sandbox calls."""

    image = "fake-image"
    name: ComputeBackend = ComputeBackend.CPU
    profiler_kind: ProfilerKind = ProfilerKind.LINUX_CPU

    def __init__(self) -> None:
        self.sandbox = MagicMock()
        self.calls: list[tuple[SandboxKind, dict[str, Any]]] = []

    def make_sandbox(self, kind: SandboxKind, **kwargs: Any) -> SandboxBackendProtocol:  # noqa: ANN401  # tracked: #288
        self.calls.append((kind, kwargs))
        if kind is SandboxKind.DOCKER:
            # A real DockerSandbox derives agent_path from (host_workspace,
            # resources); give the mock the same behavior so AgentPaths
            # assertions exercise the real lookup instead of a bare Mock.
            self.sandbox.agent_path.side_effect = _fake_agent_path(
                kwargs.get("host_workspace", ""), kwargs.get("resources", ())
            )
        return self.sandbox

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:  # noqa: ARG002  # tracked: #288
        return None

    def reselect_device(self) -> None:
        return


def _request(tmp_path: Path, backend: FakeBackend, **overrides: Any) -> RunEnvironmentRequest:  # noqa: ANN401  # tracked: #288
    workspace = overrides.pop("workspace", tmp_path / "workspace")
    workspace.mkdir(exist_ok=True)
    values: dict[str, Any] = dict(  # noqa: C408  # tracked: #288
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=backend,
        agent_backend="deepagents",
        cli_provider=None,
        run_id="run-123",
    )
    values.update(overrides)
    values["log_dir"].mkdir(exist_ok=True)
    return RunEnvironmentRequest(**values)


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _run_rootless_rust_setup(
    tmp_path: Path,
    *,
    downloader_exit_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "rustc",
        "#!/bin/sh\nprintf '%s\\n' 'rustc 1.85.0 (fake)'\n",
    )
    _write_executable(
        fake_bin / "cargo",
        "#!/bin/sh\necho 'rustup has no configured default toolchain' >&2\nexit 1\n",
    )

    rustup_init = tmp_path / "fake-rustup-init"
    working_cargo = tmp_path / "working-cargo"
    working_rustc = tmp_path / "working-rustc"
    _write_executable(
        working_cargo,
        "#!/bin/sh\nprintf '%s\\n' 'cargo 1.92.0 (fake)'\n",
    )
    _write_executable(
        working_rustc,
        "#!/bin/sh\nprintf '%s\\n' 'rustc 1.92.0 (fake)'\n",
    )
    _write_executable(
        rustup_init,
        "#!/bin/sh\n"
        'mkdir -p "$CARGO_HOME/bin" "$RUSTUP_HOME"\n'
        'cp "$FAKE_WORKING_CARGO" "$CARGO_HOME/bin/cargo"\n'
        'cp "$FAKE_WORKING_RUSTC" "$CARGO_HOME/bin/rustc"\n'
        'chmod +x "$CARGO_HOME/bin/cargo" "$CARGO_HOME/bin/rustc"\n',
    )
    downloader = (
        '#!/bin/sh\ncp "$FAKE_RUSTUP_INIT" "$4"\n'
        if downloader_exit_code == 0
        else f"#!/bin/sh\nexit {downloader_exit_code}\n"
    )
    _write_executable(fake_bin / "python3", downloader)

    backend = FakeBackend()
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    request = _request(tmp_path, backend, evaluator_package_root=package.root)
    commands = _evaluator_container_setup(request, rootless=True)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RUSTUP_INIT": str(rustup_init),
        "FAKE_WORKING_CARGO": str(working_cargo),
        "FAKE_WORKING_RUSTC": str(working_rustc),
    }
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", "set -e\n" + "\n".join((*commands, "cargo --version"))],
        cwd=request.workspace,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


#: The profiles these tests drive container setup with. agentshim registers
#: `claude` only in the release VibeSys builds against, so `codex` is supplied
#: here rather than depending on when the library ships it.
_CLI_PROFILES = {
    "claude": fake_profiles.profile(
        "claude",
        state_dirs=(".claude", ".claude.json", ".config/claude"),
        auth_files=(".claude/.credentials.json", ".claude.json"),
        auth_env_vars=(
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
        ),
        container_install=("install-claude",),
    ),
    "codex": fake_profiles.profile(
        "codex",
        state_dirs=(".codex", ".config/codex"),
        auth_files=(".codex/auth.json",),
        auth_env_vars=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
        container_install=("install-codex",),
    ),
}


@pytest.fixture(autouse=True)
def fake_agent_image(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the real Docker build behind ``agent_image`` for every Docker-path test.

    ``DockerEnvironment.open()`` always resolves an agent image before
    starting the container now; letting it shell out to a real ``docker
    build`` would make these unit tests into (slow, Docker-dependent)
    integration tests. Returns the recorded calls so a test can assert on the
    base image and toolchains it was asked to build.
    """
    calls: list[dict[str, Any]] = []

    def fake_agent_image(
        base_image: str,
        *,
        toolchains: Any = (),  # noqa: ANN401  # tracked: #288
        pip_extras: Any = (),  # noqa: ANN401  # tracked: #288
        **_kwargs: Any,  # noqa: ANN401  # tracked: #288
    ) -> str:
        calls.append(
            {
                "base_image": base_image,
                "toolchains": frozenset(toolchains),
                "pip_extras": frozenset(pip_extras),
            }
        )
        return "sha256:" + "a" * 64

    monkeypatch.setattr("vibesys.sandbox.images.agent_image", fake_agent_image)
    return calls


_PUSHED_DIGEST = "ghcr.io/uw-syfi/vibesys-agent@sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def fake_ensure_pushed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the registry push behind ``ensure_pushed`` for Modal/SkyPilot tests.

    A real push hits the network and a logged-in Docker daemon; letting it
    run would make these unit tests flaky integration tests. Returns the
    image IDs it was asked to push, and always reports the same
    ``repository@sha256:...``-shaped reference, so a test can assert a
    Modal or SkyPilot sandbox is started from *that* digest.
    """
    calls: list[str] = []

    def fake_ensure_pushed(image_id: str, **_kwargs: Any) -> str:  # noqa: ANN401  # tracked: #288
        calls.append(image_id)
        return _PUSHED_DIGEST

    monkeypatch.setattr("vibesys.sandbox.images.ensure_pushed", fake_ensure_pushed)
    return calls


@pytest.fixture(autouse=True)
def _synthetic_cli_auth(monkeypatch):  # noqa: ANN001, ANN202
    """Pin a deterministic host auth source for container CLI setup.

    ``_cli_container_env`` fails loud when a provider has neither a staged
    host file nor an auth environment variable, so these tests must not depend
    on whichever CLI the developer running them happens to be logged into.
    """
    fake_profiles.install(monkeypatch, _CLI_PROFILES)
    for profile in _CLI_PROFILES.values():
        for name in profile.auth_env_vars:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")


def test_cli_compatibility_flags_keep_options_scoped_to_selected_environment():  # noqa: ANN201  # tracked: #288
    assert make_run_environment_spec().options == {}
    assert make_run_environment_spec(use_docker=True, docker_image="editor").options == {
        "image": "editor"
    }

    remote = make_run_environment_spec(
        use_modal=True,
        docker_image="editor",
        modal_gpu="accelerator",
        modal_model_volume="weights",
        modal_app="candidate",
    )
    assert remote.name == "modal"
    assert remote.options == {
        "image": "editor",
        "gpu": "accelerator",
        "model_volume": "weights",
        "app": "candidate",
    }

    configured = make_run_environment_spec(
        use_modal=True,
        modal_entrypoint="examples/deployment/service.py",
    )
    assert configured.options["entrypoint"] == "examples/deployment/service.py"

    resources = RunResourceRequest(nodes=1, accelerators_per_node=4, accelerator_backend="rocm")
    skypilot = make_run_environment_spec(
        use_skypilot=True,
        cluster_profile="remote-gpu",
        cluster_profiles_file=Path("/operator/clusters.toml"),
        skypilot_executable="sky-custom",
        resources=resources,
    )
    assert skypilot.name == "skypilot"
    assert skypilot.options["profile"] == "remote-gpu"
    assert skypilot.options["profiles_file"] == Path("/operator/clusters.toml")
    assert skypilot.options["executable"] == "sky-custom"


def test_skypilot_selection_requires_profile_and_resources() -> None:
    resources = RunResourceRequest(accelerators_per_node=1, accelerator_backend="rocm")
    with pytest.raises(ValueError, match="cluster-profile"):
        make_run_environment_spec(use_skypilot=True, resources=resources)
    with pytest.raises(ValueError, match=r"\[resources\]"):
        make_run_environment_spec(use_skypilot=True, cluster_profile="gpu")
    with pytest.raises(ValueError, match="mutually exclusive"):
        make_run_environment_spec(
            use_docker=True,
            use_skypilot=True,
            cluster_profile="gpu",
            resources=resources,
        )


def test_run_environment_record_captures_operator_selected_options():  # noqa: ANN201  # tracked: #288
    assert run_environment_record(make_run_environment_spec()) == RunEnvironmentRecord(name="local")
    assert run_environment_record(
        make_run_environment_spec(use_docker=True, docker_image="editor")
    ) == RunEnvironmentRecord(name="docker", image="editor")
    assert run_environment_record(
        make_run_environment_spec(
            use_modal=True,
            modal_gpu="accelerator",
            modal_model_volume="weights",
            modal_app="candidate",
            # Declared by the input bundle, so it is re-derived rather than recorded.
            modal_entrypoint="examples/deployment/service.py",
        )
    ) == RunEnvironmentRecord(
        name="modal",
        gpu="accelerator",
        model_volume="weights",
        app="candidate",
    )

    resources = RunResourceRequest(
        nodes=1,
        accelerators_per_node=4,
        accelerator_backend="rocm",
        cpus_per_node=192,
    )
    assert run_environment_record(RunEnvironmentSpec(resources=resources)) == RunEnvironmentRecord(
        name="local", resources=resources
    )


def test_run_environment_record_rejects_an_unknown_environment():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="unknown run environment"):
        run_environment_record(RunEnvironmentSpec("kubernetes"))


def _modal_runtime_document(tmp_path: Path) -> str:
    return (tmp_path / "logs" / "runtime-environment.md").read_text()


def test_local_environment_opens_local_sandbox_with_host_paths(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("local"))

    session = env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
        )
    )

    assert backend.calls[0][0] is SandboxKind.LOCAL
    assert session.sandbox is backend.sandbox
    assert session.view.paths.accuracy_command == "uv run python accuracy_checker/checker.py"
    assert session.view.paths.benchmark_command == "uv run python benchmark/benchmark.py"
    assert session.view.isolated is False
    backend.sandbox.start.assert_not_called()


def test_local_environment_materializes_effective_objective_outside_workspace(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("local"))
    effective = "Optimize the service.\n\n## Operator constraints\n\n- BF16 only\n"

    session = env.open(_request(tmp_path, backend, objective=effective))

    objective_path = Path(session.view.paths.objective)
    assert objective_path == tmp_path / "logs" / "effective-objective.md"
    assert objective_path.read_text() == effective
    assert not objective_path.is_relative_to(tmp_path / "workspace")


def test_docker_environment_opens_one_started_sandbox_with_agent_paths(
    tmp_path: Path,
    fake_agent_image: list[dict[str, Any]],
) -> None:
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))

    session = env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
        )
    )

    assert backend.calls[0][0] is SandboxKind.DOCKER
    assert session.view.isolated is True
    assert session.view.cli_sandboxed is True
    assert session.view.profile_execution == "local"
    assert session.view.paths.accuracy_command == "uv run python accuracy_checker/checker.py"
    assert session.view.paths.benchmark_command == "uv run python benchmark/benchmark.py"
    assert backend.calls[0][1]["extra_env"]["UV_CACHE_DIR"] == "/workspace/.cache/uv"
    assert backend.calls[0][1]["lifecycle_hooks"] == []
    # The container always starts from a resolved agent image, built from the
    # backend's own base image, even when the run needs no evaluator tools.
    assert backend.calls[0][1]["container_image"] == "sha256:" + "a" * 64
    assert fake_agent_image[-1]["base_image"] == backend.image
    assert fake_agent_image[-1]["toolchains"] == frozenset()
    backend.sandbox.start.assert_called_once()

    session.close()
    backend.sandbox.stop.assert_called_once()


def test_symlink_lifecycle_hooks_install_and_record_quoted_commands() -> None:
    sandbox = MagicMock()
    sandbox.execute.return_value = MagicMock(exit_code=0, output="")
    hooks = _symlink_lifecycle_hooks([("/workspace/model link", "/workspace/_mounts/model target")])

    hooks[0].before_ready(BeforeReadyContext(sandbox=sandbox))

    command = "ln -sfn '/workspace/_mounts/model target' '/workspace/model link'"
    sandbox.execute.assert_called_once_with(command)
    sandbox.save_symlink_commands.assert_called_once_with([command])


def test_symlink_lifecycle_hooks_reject_failed_setup() -> None:
    sandbox = MagicMock()
    sandbox.execute.return_value = MagicMock(exit_code=17, output="permission denied")
    hooks = _symlink_lifecycle_hooks([("/workspace/model", "/mount/model")])

    with pytest.raises(RuntimeError, match="permission denied"):
        hooks[0].before_ready(BeforeReadyContext(sandbox=sandbox))

    sandbox.save_symlink_commands.assert_not_called()


def test_isolated_environment_mounts_and_translates_evaluator_package(
    tmp_path: Path,
    fake_agent_image: list[dict[str, Any]],
) -> None:
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(name="vibesys-evaluator-queue", version="0.1.0")
    )
    command = shlex.join(
        package.command(
            "vibesys-queue",
            "check",
            "--workspace",
            "${PROJECT_ROOT}",
            "--nested-json",
            f'["go","-C","{package.root}"]',
        )
    )

    session = env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command=command,
            benchmark_command=command,
            evaluator_package_root=package.root,
        )
    )

    translated = session.view.paths.accuracy_command
    assert translated is not None
    assert str(package.root) not in translated
    assert "${PROJECT_ROOT}" not in translated
    assert "/opt/vibesys-evaluator-package" in translated
    assert "/workspace" in translated
    assert (
        str(package.root),
        "/opt/vibesys-evaluator-package",
        True,
    ) in _as_mount_tuples(backend.calls[0][1]["resources"])
    # The evaluator package's declared toolchains (go, rust) are baked into
    # the agent image at build time now, not installed per-run.
    assert "extra_init_commands" not in backend.calls[0][1]
    assert fake_agent_image[-1]["toolchains"] == frozenset({"go", "rust"})


def test_rootless_rust_setup_replaces_broken_rustup_cargo_shim(tmp_path: Path) -> None:
    result = _run_rootless_rust_setup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "cargo 1.92.0 (fake)" in result.stdout
    assert (tmp_path / "workspace" / ".bin" / "cargo").is_symlink()


def test_rootless_rust_setup_does_not_mask_download_failure(tmp_path: Path) -> None:
    result = _run_rootless_rust_setup(tmp_path, downloader_exit_code=7)

    assert result.returncode != 0
    assert "failed to download evaluator Rust toolchain" in result.stderr
    assert "cargo 1.92.0 (fake)" not in result.stdout


def test_local_environment_prepares_and_translates_evaluator_tool(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    backend.sandbox.execute.return_value = MagicMock(exit_code=0, output="", truncated=False)
    env = build_run_environment(RunEnvironmentSpec("local"))
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    command = shlex.join(
        package.command(
            "request-factory-engine",
            "--trace",
            "trace.jsonl",
            "--model",
            "m",
            "--input-file-format",
            "multimodal-independent-v1",
            "--dry-run",
        )
    )
    tools_root = tmp_path / "operator-tools"

    session = env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="true",
            benchmark_command=command,
            evaluator_package_root=package.root,
            evaluator_tools_root=tools_root,
        )
    )

    lifecycle_hooks = backend.calls[0][1]["lifecycle_hooks"]
    assert len(lifecycle_hooks) == 1
    SandboxLifecycle(lifecycle_hooks).before_ready(backend.sandbox)
    backend.sandbox.execute.assert_called_once_with(
        evaluator_tools_install_command(package.metadata.tools, tools_root),
        timeout=660,
    )
    tool = package.metadata.tools["request-factory"]
    benchmark = shlex.split(session.view.paths.benchmark_command or "")
    engine = tool_install_root(tools_root, "request-factory", tool) / "bin" / "session_runner"
    assert benchmark == [
        str(engine),
        "--trace",
        "trace.jsonl",
        "--model",
        "m",
        "--input-file-format",
        "multimodal-independent-v1",
        "--dry-run",
    ]
    assert "${TOOL:" not in (session.view.paths.benchmark_command or "")


def test_local_environment_rejects_evaluator_tools_root_inside_workspace(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("local"))
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="must be outside the candidate workspace"):
        env.open(
            _request(
                tmp_path,
                backend,
                workspace=workspace,
                accuracy_command="true",
                benchmark_command="true",
                evaluator_package_root=package.root,
                evaluator_tools_root=workspace / "cache",
            )
        )

    assert backend.calls == []


@pytest.mark.parametrize("environment_name", ["docker", "modal"])
def test_isolated_environments_install_and_translate_evaluator_tools(
    tmp_path: Path,
    environment_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    env = build_run_environment(RunEnvironmentSpec(environment_name))
    command = shlex.join(
        package.command(
            "request-factory-engine",
            "--trace",
            "trace.jsonl",
            "--dry-run",
        )
    )
    if environment_name == "docker":
        tool = package.metadata.tools["request-factory"]
        built_root = tmp_path / "built-request-factory"
        container_root = tool_install_root(
            Path("/opt/vibesys-evaluator-tools"), "request-factory", tool
        )
        monkeypatch.setattr(
            "vibesys.sandbox.run_environment._docker_evaluator_tool_mounts",
            lambda _request, _tools, **_kwargs: [(str(built_root), str(container_root), True)],
        )
        monkeypatch.setattr(
            "vibesys.sandbox.images.agent_image",
            lambda *_args, **_kwargs: "sha256:pinned",
        )

    session = env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="true",
            benchmark_command=command,
            evaluator_package_root=package.root,
        )
    )

    rendered = session.view.paths.benchmark_command or ""
    assert "${TOOL:" not in rendered
    assert "evaluator-tools" in rendered
    if environment_name == "docker":
        assert "/opt/vibesys-evaluator-tools" in rendered
        assert backend.calls[0][1]["container_image"] == "sha256:pinned"
        assert not any(
            isinstance(hook, EvaluatorToolLifecycleHooks)
            for hook in backend.calls[0][1]["lifecycle_hooks"]
        )
        assert (str(built_root), str(container_root), True) in _as_mount_tuples(
            backend.calls[0][1]["resources"]
        )
        # Cargo-git tools are prebuilt and mounted read-only; nothing installs
        # Rust in the running container.
        assert "extra_init_commands" not in backend.calls[0][1]
    else:
        # The local editor container starts from a prebuilt agent image and
        # installs nothing at start; the evaluator's own toolchain setup
        # (checked below) travels as an argument to the remote evaluator
        # helper script, not as a container init command.
        assert "extra_init_commands" not in backend.calls[0][1]
        arguments = shlex.split(rendered)
        separator = arguments.index("--")
        assert arguments[separator + 1].startswith(".vibesys-evaluator-tools/request-factory/")
        assert "--setup-command-base64" in arguments[:separator]
        encoded_setup = arguments[arguments.index("--setup-command-base64") + 1]
        setup_argv = json.loads(base64.urlsafe_b64decode(encoded_setup))
        assert setup_argv[:2] == ["sh", "-c"]
        assert "cargo" in setup_argv[2]
        assert ".vibesys-evaluator-tools" in setup_argv[2]
        assert "RUSTUP_HOME" in setup_argv[2]
        assert "CARGO_HOME" in setup_argv[2]
        assert "apt-get" not in setup_argv[2]
        assert "/root" not in setup_argv[2]
        assert setup_argv[2].index("rm -rf") < setup_argv[2].index("cargo install")
        for bootstrap_path in (".bin", ".pip", ".uv-cache"):
            assert bootstrap_path not in setup_argv[2].splitlines()[1]
        assert arguments[arguments.index("--evaluator-package-root") + 1] == (
            "/opt/vibesys-evaluator-package"
        )


def test_docker_evaluator_tools_use_ephemeral_builder_and_read_only_final_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    request = _request(
        tmp_path,
        backend,
        evaluator_package_root=package.root,
        evaluator_tools_root=tmp_path / "operator-tools",
    )
    prepare_calls = 0

    def prepare(tools, install_parent, *, command_runner=None):  # noqa: ANN001, ANN202
        nonlocal prepare_calls
        del tools, install_parent, command_runner
        prepare_calls += 1
        if prepare_calls == 1:
            raise _EvaluatorToolBuildRequiredError
        return {}

    monkeypatch.setattr("vibesys.sandbox.run_environment.prepare_evaluator_tools", prepare)
    backend.sandbox.execute.return_value = MagicMock(exit_code=0, output="")

    mounts = _docker_evaluator_tool_mounts(
        request,
        package.metadata.tools,
        container_image="sha256:pinned",
    )

    assert prepare_calls == 2
    assert len(backend.calls) == 1
    kind, kwargs = backend.calls[0]
    assert kind is SandboxKind.DOCKER
    assert kwargs["ephemeral"] is True
    assert kwargs["attach_accelerator"] is False
    assert kwargs["container_image"] == "sha256:pinned"
    assert len(kwargs["lifecycle_hooks"]) == 1
    assert isinstance(kwargs["lifecycle_hooks"][0], EvaluatorToolLifecycleHooks)
    assert any(
        "static.rust-lang.org/rustup/dist" in command for command in kwargs["extra_init_commands"]
    )
    assert kwargs["bind_mounts"][0][2] is False
    assert all(read_only for _, _, read_only in mounts)
    backend.sandbox.start.assert_called_once_with()
    ownership_command = backend.sandbox.execute.call_args.args[0]
    ownership_argv = shlex.split(ownership_command)
    assert ownership_argv[:2] == ["sh", "-c"]
    assert "stat -c" in ownership_argv[2]
    assert "chown -R" in ownership_argv[2]
    assert ".host-owner-" in ownership_argv[4]
    backend.sandbox.stop.assert_called_once_with()


def test_docker_tool_cache_resolves_existing_image_content_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspect = MagicMock(return_value=MagicMock(returncode=0, stdout="sha256:abc\n"))
    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", inspect)

    assert _resolve_docker_image_id("example:latest") == "sha256:abc"
    inspect.assert_called_once()


def test_docker_tool_cache_pulls_then_pins_missing_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock(
        side_effect=[
            MagicMock(returncode=1, stdout="", stderr="missing"),
            MagicMock(returncode=0, stdout="pulled", stderr=""),
            MagicMock(returncode=0, stdout="sha256:resolved\n", stderr=""),
        ]
    )
    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", run)

    assert _resolve_docker_image_id("example:latest") == "sha256:resolved"
    assert run.call_args_list[1].args[0] == ["docker", "image", "pull", "example:latest"]


def test_environment_quotes_project_root_after_token_expansion(tmp_path: Path) -> None:
    backend = FakeBackend()
    workspace = tmp_path / "candidate's; touch injected"
    workspace.mkdir()
    env = build_run_environment(RunEnvironmentSpec("local"))

    request = _request(
        tmp_path,
        backend,
        workspace=workspace,
        accuracy_command="python checker.py --workspace '${PROJECT_ROOT}'",
        benchmark_command="true",
    )
    session = env.open(request)

    command = session.view.paths.accuracy_command
    assert command is not None
    assert shlex.split(command) == [
        "python",
        "checker.py",
        "--workspace",
        str(request.workspace),
    ]


def test_environment_quotes_nested_shell_paths(tmp_path: Path) -> None:
    backend = FakeBackend()
    workspace = tmp_path / "candidate's; touch injected"
    workspace.mkdir()
    vibesys_project = Project.open(NESTED_SHELL_PROJECT)
    bundle = load_project_task(vibesys_project, vibesys_project.select_task("nested-shell"))
    env = build_run_environment(RunEnvironmentSpec("local"))

    session = env.open(
        _request(
            tmp_path,
            backend,
            workspace=workspace,
            accuracy_command=bundle.accuracy_command_display,
            benchmark_command=bundle.benchmark_command_display,
            evaluator_package_root=bundle.evaluator_package_root,
        )
    )

    command = session.view.paths.benchmark_command
    assert command is not None
    outer = shlex.split(command)
    nested = json.loads(outer[outer.index("--run-command-json") + 1])
    assert nested[5] == str(workspace / "service" / "docker-compose.yml")
    assert "${PROJECT_ROOT}" not in json.dumps(nested)


@pytest.mark.parametrize(
    "nested",
    [
        '["sh","-c","printf \\"%s\\" \\"${PROJECT_ROOT}\\""]',
        '["bash","-ec","printf %s ${PROJECT_ROOT}"]',
        '["bash","-o","pipefail","-c","printf %s ${PROJECT_ROOT}"]',
        '["/usr/bin/bash","-c","printf %s ${PROJECT_ROOT}"]',
        '["env","sh","-c","printf %s ${PROJECT_ROOT}"]',
        '["/usr/bin/env","-i","MODE=test","bash","-ec","printf %s ${PROJECT_ROOT}"]',
        '["env","-S","sh -c \'printf %s ${PROJECT_ROOT}\'"]',
    ],
)
def test_environment_rejects_semantic_tokens_in_nested_shell_source(
    tmp_path: Path,
    nested: str,
) -> None:
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("local"))

    with pytest.raises(ValueError, match="positional arguments"):
        env.open(
            _request(
                tmp_path,
                backend,
                accuracy_command=shlex.join(["checker", "--run-command-json", nested]),
                benchmark_command="true",
            )
        )


@pytest.mark.parametrize(
    "command",
    [
        ["sh", "-c", "printf %s ${PROJECT_ROOT}"],
        ["python", "-c", "print('${PROJECT_ROOT}')"],
        ["node", "--eval", "console.log('${PROJECT_ROOT}')"],
    ],
)
def test_environment_rejects_semantic_tokens_in_top_level_executable_source(
    tmp_path: Path,
    command: list[str],
) -> None:
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("local"))

    with pytest.raises(ValueError, match="positional arguments"):
        env.open(
            _request(
                tmp_path,
                backend,
                accuracy_command=shlex.join(command),
                benchmark_command="true",
            )
        )


def test_microservice_package_does_not_install_rust(
    tmp_path: Path,
    fake_agent_image: list[dict[str, Any]],
) -> None:
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-microservice",
            version="0.1.0",
        )
    )

    env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="true",
            benchmark_command="true",
            evaluator_package_root=package.root,
        )
    )

    assert fake_agent_image[-1]["toolchains"] == frozenset({"go"})


def test_docker_environment_mounts_effective_objective_read_only(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    effective = "Optimize.\n\n## Operator constraints\n\n- exact BF16\n"

    session = env.open(_request(tmp_path, backend, objective=effective))

    host_path = tmp_path / "logs" / "effective-objective.md"
    assert host_path.read_text() == effective
    assert (
        str(host_path),
        "/opt/vibesys-runtime/objective.md",
        True,
    ) in _as_mount_tuples(backend.calls[0][1]["resources"])
    assert "/opt/vibesys-runtime" in backend.calls[0][1]["passthrough_paths"]
    assert session.view.paths.objective == "/opt/vibesys-runtime/objective.md"


def test_local_environment_objective_defaults_to_the_bare_workspace_relative_name(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """The host answer for an unset objective is identity: no lookup, no rewrite."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("local"))

    session = env.open(_request(tmp_path, backend))

    assert session.view.paths.objective == "OBJECTIVE.md"


def test_container_mount_plan_declares_named_resources_with_agent_paths(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """``_container_mount_plan`` returns ``HostResource`` entries, not bind-mount
    tuples, with ``access`` and ``agent_path`` set for every named mount."""
    backend = FakeBackend()
    history = tmp_path / "experiment-history"
    history.mkdir()
    package_root = tmp_path / "evaluator-package"
    package_root.mkdir()
    request = _request(
        tmp_path,
        backend,
        objective="Optimize the service.\n",
        git_history_root=history,
        evaluator_package_root=package_root,
        agent_backend="cli",
        cli_provider="codex",
    )

    resources, _symlinks, passthrough = _container_mount_plan(request)

    assert all(isinstance(resource, HostResource) for resource in resources)
    by_agent_path = {resource.agent_path: resource for resource in resources}

    objective_resource = by_agent_path["/opt/vibesys-runtime/objective.md"]
    assert objective_resource.access is HostResourceAccess.READ_ONLY
    assert objective_resource.path == tmp_path / "logs" / "effective-objective.md"

    history_resource = by_agent_path["/opt/vibesys-history"]
    assert history_resource.access is HostResourceAccess.READ_ONLY
    assert history_resource.path == history

    package_resource = by_agent_path["/opt/vibesys-evaluator-package"]
    assert package_resource.access is HostResourceAccess.READ_ONLY
    assert package_resource.path == package_root

    framework_resource = by_agent_path["/opt/vibesys"]
    assert framework_resource.access is HostResourceAccess.READ_ONLY
    assert framework_resource.path == request.framework_root

    assert "/opt/vibesys-runtime" in passthrough
    assert "/opt/vibesys-history" in passthrough


@pytest.mark.parametrize("environment_name", ["docker", "modal"])
def test_isolated_environment_enforces_project_path_policy(tmp_path, environment_name):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec(environment_name))
    project = tmp_path / "workspace"
    (project / ".git").mkdir(parents=True)
    (project / ".state" / "local").mkdir(parents=True)
    (project / ".state" / "project.json").write_text("{}\n")
    (project / "vibesys.input.toml").write_text("version = 1\n")
    (project / "agent.toml").write_text("[model]\nname = 'private'\n")
    policy = ProjectPathPolicy(
        read_only_paths=(".git", ".state", "vibesys.input.toml"),
        hidden_paths=(".state/local", "agent.toml"),
    )

    env.open(
        _request(
            tmp_path,
            backend,
            agent_backend="cli",
            cli_provider="codex",
            project_path_policy=policy,
        )
    )

    mounts = _as_mount_tuples(backend.calls[0][1]["resources"])
    assert (str(project / ".git"), "/workspace/.git", True) in mounts
    assert (str(project / ".state"), "/workspace/.state", True) in mounts
    assert (
        str(project / "vibesys.input.toml"),
        "/workspace/vibesys.input.toml",
        True,
    ) in mounts
    hidden_mounts = {
        container: Path(host)
        for host, container, read_only in mounts
        if read_only and container in {"/workspace/.state/local", "/workspace/agent.toml"}
    }
    assert hidden_mounts["/workspace/.state/local"].is_dir()
    assert hidden_mounts["/workspace/agent.toml"].is_file()
    assert hidden_mounts["/workspace/.state/local"].is_relative_to(tmp_path / "logs")
    assert hidden_mounts["/workspace/agent.toml"].is_relative_to(tmp_path / "logs")


def test_cli_container_env_and_setup_agree_on_the_container_environment(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    """``_cli_provider_env_and_auth_files`` (Modal/SkyPilot) agrees with
    ``_cli_container_env`` (the plain Docker path) on the container
    environment, and additionally returns the staged auth copy pairs a
    prebuilt-image container needs instead of a shell install recipe.
    """
    backend = FakeBackend()
    home = tmp_path / "synthetic-home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('{"synthetic": true}\n')
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    request = _request(tmp_path, backend, agent_backend="cli", cli_provider="codex")

    resolved = _cli_container_env(request)
    env, auth_files = _cli_provider_env_and_auth_files(request)

    assert resolved is not None
    provider, resolved_env = resolved
    assert provider == "codex"
    assert env == resolved_env
    assert auth_files == [("/opt/vibesys-auth/0", "/home/agent/.codex/auth.json")]


def test_cli_container_env_is_none_for_a_non_cli_agent_backend(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    request = _request(tmp_path, backend, agent_backend="deepagents", cli_provider="codex")

    assert _cli_container_env(request) is None


def test_docker_agent_toolchains_adds_rust_only_when_tools_are_needed(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    request = _request(tmp_path, backend, evaluator_package_root=package.root)

    assert "rust" in _docker_agent_toolchains(request, _evaluator_tools(request))
    assert _docker_agent_toolchains(_request(tmp_path, backend), {}) == frozenset()


def test_docker_environment_copies_cli_auth_from_readonly_staging(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    home = tmp_path / "synthetic-home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('{"synthetic": true}\n')
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    kwargs = backend.calls[0][1]
    assert (str(auth_file), "/opt/vibesys-auth/0", True) in _as_mount_tuples(kwargs["resources"])
    # The plain Docker path copies staged auth files into the agent HOME as a
    # start-time DockerSandbox step, not via extra_init_commands.
    assert "extra_init_commands" not in kwargs
    assert kwargs["auth_files"] == [("/opt/vibesys-auth/0", "/home/agent/.codex/auth.json")]


def test_docker_environment_forwards_host_cli_auth_environment(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "proxy-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_MODEL", "host-selected-model")

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="claude"))

    container_env = backend.calls[0][1]["extra_env"]
    assert container_env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"  # noqa: S105  # tracked: #288
    assert container_env["ANTHROPIC_BASE_URL"] == "https://proxy.invalid/v1"
    # VibeSys owns per-role model selection, so a host export must not reach
    # the container and override it.
    assert "ANTHROPIC_MODEL" not in container_env
    # The agent image runs the CLI as the non-root "agent" user, so Claude's
    # root-only IS_SANDBOX=1 escape hatch is no longer needed or forwarded.
    assert "IS_SANDBOX" not in container_env
    assert container_env["PYTHONPATH"] == "/opt/vibesys"


def test_docker_environment_rejects_a_cli_provider_without_any_auth_source(  # noqa: ANN201  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
    monkeypatch,  # noqa: ANN001  # tracked: #288
):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    home = tmp_path / "absent-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="no 'codex' CLI authentication") as excinfo:
        env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    message = str(excinfo.value)
    assert str(home / ".codex" / "auth.json") in message
    assert "OPENAI_API_KEY" in message
    assert "OPENAI_BASE_URL" in message
    assert backend.calls == []


def test_docker_environment_exposes_framework_git_history_read_only(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    history = tmp_path / "experiment-history"
    history.mkdir()

    session = env.open(
        _request(
            tmp_path,
            backend,
            agent_backend="cli",
            cli_provider="codex",
            git_history_root=history,
        )
    )

    kwargs = backend.calls[0][1]
    assert (str(history), "/opt/vibesys-history", True) in _as_mount_tuples(kwargs["resources"])
    assert "/opt/vibesys-history" in kwargs["passthrough_paths"]
    assert kwargs["extra_env"]["VIBESYS_GIT_HISTORY"] == "/opt/vibesys-history"
    assert "/opt/vibesys-history" in session.view.prompt_notes
    assert "hashes without recoverable source are insufficient" in session.view.prompt_notes


def test_docker_environment_uses_environment_bind_mounts(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    env.open(
        _request(
            tmp_path,
            backend,
            environment_bind_mounts=(EnvironmentBindMount(model_dir, "/model", True),),  # noqa: FBT003  # tracked: #288
        )
    )

    kwargs = backend.calls[0][1]
    assert (str(model_dir), "/model", True) in _as_mount_tuples(kwargs["resources"])
    assert "/model" in kwargs["passthrough_paths"]


def test_docker_environment_mounts_selected_profiler_support(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    support = tmp_path / "custom-profiler"
    support.mkdir()

    session = env.open(
        _request(
            tmp_path,
            backend,
            profiler_support_path=str(support),
            profiler_support_name="fixture_profiler",
        )
    )

    kwargs = backend.calls[0][1]
    assert (str(support), "/workspace/fixture_profiler", True) in _as_mount_tuples(
        kwargs["resources"]
    )
    assert session.view.paths.profiler_support == "fixture_profiler"


def test_docker_environment_does_not_infer_model_mount_from_reference_dir(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    ref_dir = tmp_path / "reference"
    (ref_dir / "model").mkdir(parents=True)

    env.open(_request(tmp_path, backend, ref_dir=ref_dir))

    mounts = _as_mount_tuples(backend.calls[0][1]["resources"])
    assert all(container_path != "/model" for _, container_path, _ in mounts)


def test_environment_session_context_manager_closes(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))

    with env.open(_request(tmp_path, backend)) as session:
        assert session.sandbox is backend.sandbox
        backend.sandbox.stop.assert_not_called()

    backend.sandbox.stop.assert_called_once()
    session.close()
    backend.sandbox.stop.assert_called_once()


def test_modal_environment_uses_local_docker_for_editing(
    tmp_path,  # noqa: ANN001  # tracked: #288
    fake_agent_image: list[dict[str, Any]],
    fake_ensure_pushed: list[str],
) -> None:
    """Post-refactor (April 2026): Modal mode runs the agent in a local
    Docker container; only GPU-bound work the implementer dispatches via
    `modal run` actually touches Modal."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    # The sandbox is local Docker, not a Modal Sandbox.
    assert backend.calls[0][0] is SandboxKind.DOCKER
    assert backend.calls[0][1]["attach_accelerator"] is False
    # The container starts from the same kind of agent image the plain
    # Docker path builds, pushed to and pulled back from a registry.
    assert fake_agent_image[-1]["base_image"] == backend.image
    assert fake_ensure_pushed == ["sha256:" + "a" * 64]
    assert backend.calls[0][1]["container_image"] == _PUSHED_DIGEST
    assert session.view.cli_sandboxed is True
    assert session.view.profile_execution == "remote"
    assert session.view.deployment_namespace is not None
    assert session.view.supports_parallel_candidate_evaluation is True
    assert session.view.deployment_release_env_var == "VIBESYS_RELEASE_MODAL_DEPLOYMENT"
    backend.sandbox.start.assert_called_once()


def test_modal_environment_owns_candidate_runtime_naming(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))
    session = env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    runtime = env.candidate_runtime(session.view, generation=12, child_idx=7)

    assert runtime.deployment_name is not None
    assert runtime.deployment_name.endswith("-g12c7")
    assert len(runtime.deployment_name) <= 63
    assert session.view.deployment_namespace is not None
    assert session.view.deployment_namespace in runtime.prompt_notes
    assert "Candidate-specific namespace override" in runtime.prompt_notes
    assert runtime.deployment_name in runtime.prompt_notes


def test_modal_environment_wraps_service_evaluators_with_remote_dispatch(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(
        _request(
            tmp_path,
            backend,
            agent_backend="cli",
            cli_provider="codex",
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py --concurrency 16",
        )
    )

    helper = "/opt/vibesys-modal-evaluator.py"
    prefix = f"python {helper} --readiness-timeout-seconds 1200 --"
    assert session.view.paths.accuracy_command == (
        f"{prefix} uv run python accuracy_checker/checker.py"
    )
    assert session.view.paths.benchmark_command == (
        f"{prefix} uv run python benchmark/benchmark.py --concurrency 16"
    )
    assert session.view.framework_setup_timeout_seconds == 1200
    assert any(
        container_path == helper and read_only
        for _, container_path, read_only in _as_mount_tuples(backend.calls[0][1]["resources"])
    )


def test_modal_environment_wraps_custom_deployment_entrypoint(tmp_path) -> None:  # noqa: ANN001
    backend = FakeBackend()
    env = build_run_environment(
        RunEnvironmentSpec(
            "modal",
            {"entrypoint": "examples/deployment/service with space.py"},
        )
    )

    session = env.open(
        _request(
            tmp_path,
            backend,
            agent_backend="cli",
            cli_provider="codex",
            accuracy_command="trusted-check",
            benchmark_command="trusted-benchmark",
        )
    )

    helper = "/opt/vibesys-modal-evaluator.py"
    prefix = (
        f"python {helper} --entrypoint 'examples/deployment/service with space.py' "
        "--readiness-timeout-seconds 1200 --"
    )
    assert session.view.paths.accuracy_command == f"{prefix} trusted-check"
    assert session.view.paths.benchmark_command == f"{prefix} trusted-benchmark"


def test_modal_environment_installs_nothing_at_container_start(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """The local Docker container starts from a prebuilt, pushed agent image
    and installs nothing at start, including the Modal Python SDK an earlier
    revision `pip install`ed here: that install ran through
    `extra_init_commands`, which `DockerSandbox` has ignored since the
    agent-image work landed (#675), so it was already dead code."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    assert "extra_init_commands" not in backend.calls[0][1]
    assert backend.calls[0][1]["extra_env"]["UV_CACHE_DIR"] == "/workspace/.cache/uv"


def test_modal_environment_prompt_references_runtime_document(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """Prompts name the runtime manual instead of embedding it in every role."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    notes = session.view.prompt_notes
    runtime = _modal_runtime_document(tmp_path)

    assert notes == (
        "Runtime instructions are at `/opt/vibesys-runtime/environment.md`. Read that "
        "file before executing, deploying, benchmarking, or profiling; it contains "
        "the authoritative environment and lifecycle rules."
    )
    assert "modal run" not in notes
    assert "modal run" in runtime
    assert "@app.cls" in runtime or "@app.function" in runtime
    assert "GPU" in runtime
    assert any(
        container_path == "/opt/vibesys-runtime/environment.md" and read_only
        for _, container_path, read_only in _as_mount_tuples(backend.calls[0][1]["resources"])
    )
    assert "/opt/vibesys-runtime" in backend.calls[0][1]["passthrough_paths"]
    # Tell the agent where to look up volume names rather than baking them in.
    assert "meta.json" in runtime
    # No hardcoded model IDs or vibesys-internal volume names should leak
    # into the runtime-notes block.
    forbidden = (
        "yuhuili",
        "Llama-3",
        "vibesys-model-meta-llama",
        "vibesys-model-yuhuili",
    )
    for token in forbidden:
        assert token not in runtime, f"runtime manual leaks task-specific token {token!r}"
    prior_solution_terms = (
        "EAGLE3",
        "speculative decoding",
        "CUDA graphs",
        "FlashAttention",
        "continuous batching",
        "paged attention",
    )
    for term in prior_solution_terms:
        assert term.casefold() not in runtime.casefold()


def test_modal_environment_mounts_effective_objective_read_only(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))
    effective = "Optimize.\n\n## Operator constraints\n\n- no quantization\n"

    session = env.open(
        _request(
            tmp_path,
            backend,
            agent_backend="cli",
            cli_provider="codex",
            objective=effective,
        )
    )

    host_path = tmp_path / "logs" / "effective-objective.md"
    assert host_path.read_text() == effective
    assert (
        str(host_path),
        "/opt/vibesys-runtime/objective.md",
        True,
    ) in _as_mount_tuples(backend.calls[0][1]["resources"])
    assert session.view.paths.objective == "/opt/vibesys-runtime/objective.md"


def test_modal_environment_prompt_notes_require_remote_runtime_fingerprint(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))
    notes = _modal_runtime_document(tmp_path)

    assert "authoritative runtime" in notes
    assert "runtime fingerprint" in notes
    assert "must not be used to infer remote compatibility" in notes
    assert "same Modal image and hardware" in notes


def test_modal_environment_requires_exact_default_h100_identity(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))
    notes = _modal_runtime_document(tmp_path)

    assert "gpu='H100!'" in notes
    assert "Accelerator identity is an experimental contract" in notes
    assert "Modal may upgrade bare `H100` requests to H200" in notes
    assert "fail closed" in notes


def test_modal_environment_documents_history_and_exact_measurement_source(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))
    history = tmp_path / "experiment-history"
    history.mkdir()

    env.open(
        _request(
            tmp_path,
            backend,
            agent_backend="cli",
            cli_provider="codex",
            git_history_root=history,
        )
    )
    kwargs = backend.calls[0][1]
    notes = _modal_runtime_document(tmp_path)

    assert (str(history), "/opt/vibesys-history", True) in _as_mount_tuples(kwargs["resources"])
    assert kwargs["extra_env"]["VIBESYS_GIT_HISTORY"] == "/opt/vibesys-history"
    assert "git -c safe.directory=/opt/vibesys-history" in notes
    assert "ls-tree -r --name-only <commit>" in notes
    assert "Do not run `git checkout`" in notes
    assert "preserving Git HEAD, roadmap/progress/Pareto" in notes
    assert "manifest containing only per-file hashes is not sufficient" in notes
    assert "Create this provenance artifact before launch" in notes


def test_modal_environment_uses_explicit_run_id_for_namespace(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """Project location does not participate in remote resource identity."""
    backend_a = FakeBackend()
    backend_b = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    ws_a = tmp_path / "projects" / "queue-a"
    ws_a.mkdir(parents=True)
    ws_b = tmp_path / "projects" / "queue-b"
    ws_b.mkdir(parents=True)

    log_a = tmp_path / "logsA"
    log_a.mkdir(exist_ok=True)
    log_b = tmp_path / "logsB"
    log_b.mkdir(exist_ok=True)

    req_a = RunEnvironmentRequest(
        log_dir=log_a,
        workspace=ws_a,
        ref_dir=None,
        backend=backend_a,
        agent_backend="cli",
        cli_provider="codex",
        run_id="20260429-100000-runa",
    )
    req_b = RunEnvironmentRequest(
        log_dir=log_b,
        workspace=ws_b,
        ref_dir=None,
        backend=backend_b,
        agent_backend="cli",
        cli_provider="codex",
        run_id="20260429-100100-runb",
    )
    env.open(req_a)
    env.open(req_b)
    notes_a = (log_a / "runtime-environment.md").read_text()
    notes_b = (log_b / "runtime-environment.md").read_text()

    assert "vibesys-20260429-100000-runa" in notes_a
    assert "vibesys-20260429-100100-runb" in notes_b
    assert "vibesys-20260429-100000-runa" not in notes_b
    assert "vibesys-20260429-100100-runb" not in notes_a


def test_modal_environment_runtime_notes_describe_profile_contract(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """The runtime notes must spell out the modal_profile / profile_remote
    contract; without it the profiler agent has no Modal entrypoint to
    invoke and falls back to local synthetic-weight profiling."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))
    notes = _modal_runtime_document(tmp_path)

    assert "modal_profile" in notes
    assert "profile_remote" in notes
    assert "@app.local_entrypoint()" in notes
    assert "torch.profiler" in notes
    # Schema reference for the analyzer-compatible JSON shape.
    assert "analyze_torch_profile.py" in notes
    assert "_summarize_prof" in notes
    assert "total_cuda_time_us" not in notes
    assert "from torch.autograd import DeviceType" not in notes


def test_modal_environment_prompt_notes_reuse_workspace_uv_cache(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))
    notes = _modal_runtime_document(tmp_path)

    assert "UV_CACHE_DIR=/workspace/.cache/uv" in notes
    assert "persist outside Git checkpoints" in notes
    assert "do not delete or recreate `.venv`" in notes
    assert ".venv/bin/python -m ..." in notes
    assert "excluding `.venv` and `.cache`" in notes


def test_modal_environment_with_deepagents_uses_docker_too(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """The deepagents path also runs locally in Docker now — Modal is a
    dispatch target, not a runtime for the agent."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    env.open(_request(tmp_path, backend, agent_backend="deepagents"))

    assert backend.calls[0][0] is SandboxKind.DOCKER


def test_unknown_environment_name_raises():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="unknown run environment"):
        build_run_environment(RunEnvironmentSpec("wat"))


def test_skypilot_environment_uses_cpu_editor_and_narrow_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_agent_image: list[dict[str, Any]],
    fake_ensure_pushed: list[str],
) -> None:
    profiles = tmp_path / "clusters.toml"
    profiles.write_text(
        """schema_version = 1
[profiles.gpu]
runner = "skypilot"
infra = "slurm/example/gpu"
accelerator_backend = "rocm"
accelerator_type = "MI300A"
accelerators_per_node = 4
remote_artifact_root = "/remote/vibesys"
"""
    )
    resources = RunResourceRequest(nodes=1, accelerators_per_node=4, accelerator_backend="rocm")
    captures: dict[str, object] = {}

    class FakeBridge:
        def __init__(self, **kwargs):  # noqa: ANN003, ANN204
            captures.update(kwargs)
            self.socket_path = kwargs["socket_path"]
            self.closed = 0

        def start(self) -> None:
            self.socket_path.write_text("socket")

        def close(self) -> None:
            self.closed += 1

    monkeypatch.setattr("vibesys.sandbox.run_environment.SkyPilotBridge", FakeBridge)
    backend = FakeBackend()
    environment = build_run_environment(
        make_run_environment_spec(
            use_skypilot=True,
            cluster_profile="gpu",
            cluster_profiles_file=profiles,
            resources=resources,
        )
    )

    session = environment.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="python checker.py ${PROJECT_ROOT}/candidate.json",
            benchmark_command="python benchmark.py",
            benchmark_output_argument="--output-json",
            state_namespace=MagicMock(),
        )
    )

    kind, kwargs = backend.calls[0]
    assert kind is SandboxKind.DOCKER
    assert kwargs["attach_accelerator"] is False
    # The local editor container starts from the same kind of pushed agent
    # image the plain Docker path builds, and installs nothing at start; this
    # is unrelated to the accelerator job's own `image_id`, which stays the
    # operator-declared `remote_runtime_image` from the cluster profile.
    assert fake_agent_image[-1]["base_image"] == backend.image
    assert fake_ensure_pushed == ["sha256:" + "a" * 64]
    assert kwargs["container_image"] == _PUSHED_DIGEST
    assert "extra_init_commands" not in kwargs
    mounts = _as_mount_tuples(kwargs["resources"])
    assert any(target == "/opt/vibesys-skypilot/bridge.sock" for _, target, _ in mounts)
    assert any(target == "/opt/vibesys-skypilot-evaluator.py" for _, target, _ in mounts)
    assert all(".ssh" not in source and ".sky" not in source for source, _, _ in mounts)
    assert captures["commands"] == {
        "accuracy": ("python", "checker.py", "./candidate.json"),
        "benchmark": ("python", "benchmark.py"),
    }
    assert captures["benchmark_output_argument"] == "--output-json"
    assert session.view.paths.accuracy_command is not None
    assert session.view.paths.accuracy_command.endswith(" accuracy")
    assert session.view.paths.benchmark_command is not None
    assert session.view.paths.benchmark_command.endswith(" benchmark")
    assert session.view.profile_execution == "remote"
    assert session.view.supports_parallel_candidate_evaluation is False

    session.close()
    session.close()
    backend.sandbox.stop.assert_called_once()
    assert isinstance(session, _SkyPilotRunEnvironmentSession)
    bridge = session.bridge
    assert isinstance(bridge, FakeBridge)
    assert bridge.closed == 1


def test_skypilot_environment_installs_evaluator_tools_in_remote_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = tmp_path / "clusters.toml"
    profiles.write_text(
        """schema_version = 1
[profiles.gpu]
runner = "skypilot"
infra = "aws"
accelerator_backend = "cuda"
accelerator_type = "H100"
accelerators_per_node = 1
remote_artifact_root = "/remote/vibesys"
"""
    )
    captures: dict[str, object] = {}

    class FakeBridge:
        def __init__(self, **kwargs):  # noqa: ANN003, ANN204
            captures.update(kwargs)
            self.socket_path = kwargs["socket_path"]

        def start(self) -> None:
            self.socket_path.write_text("socket")

        def close(self) -> None:
            return

    monkeypatch.setattr("vibesys.sandbox.run_environment.SkyPilotBridge", FakeBridge)
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    benchmark = shlex.join(
        package.command("request-factory-engine", "--trace", "trace.jsonl", "--dry-run")
    )
    backend = FakeBackend()
    environment = build_run_environment(
        make_run_environment_spec(
            use_skypilot=True,
            cluster_profile="gpu",
            cluster_profiles_file=profiles,
            resources=RunResourceRequest(
                nodes=1,
                accelerators_per_node=1,
                accelerator_backend="cuda",
            ),
        )
    )

    session = environment.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="true",
            benchmark_command=benchmark,
            evaluator_package_root=package.root,
            state_namespace=MagicMock(),
        )
    )

    tool = package.metadata.tools["request-factory"]
    expected = (
        Path(".vibesys-evaluator-tools")
        / "request-factory"
        / tool_spec_digest(tool)
        / "bin"
        / "session_runner"
    )
    commands = captures["commands"]
    assert isinstance(commands, dict)
    assert commands["benchmark"][0] == str(expected)
    setup = captures["framework_setup_command"]
    assert isinstance(setup, str)
    assert "cargo" in setup
    assert ".vibesys-evaluator-tools" in setup
    assert "RUSTUP_HOME" in setup
    assert "CARGO_HOME" in setup
    assert "apt-get" not in setup
    assert "/root" not in setup
    assert setup.index("rm -rf") < setup.index("cargo install")
    for reserved in (".bin", ".pip", ".uv-cache"):
        assert reserved in setup.splitlines()[1]
    assert str(package.root) not in setup
    assert backend.calls[0][1]["lifecycle_hooks"] == []
    session.close()


def test_docker_remove_workspace_child_quotes_path(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    calls = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001  # tracked: #288
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = b""
        return result

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    ok = env.remove_workspace_child(
        tmp_path,
        "semi;touch hacked",
        backend=backend,
    )

    assert ok is True
    shell_command = calls[0][-1]
    assert "rm -rf -- " in shell_command
    assert "'/workspace/semi;touch hacked'" in shell_command


def test_modal_teardown_deployment_stops_app_via_cli(monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    import sys as _sys  # noqa: PLC0415  # tracked: #288

    env = build_run_environment(RunEnvironmentSpec("modal"))
    calls = []
    logs = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001  # tracked: #288
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    env.teardown_deployment("vibesys-run-g1c2", log=logs.append)

    assert calls == [[_sys.executable, "-m", "modal", "app", "stop", "vibesys-run-g1c2", "--yes"]]
    assert any("stopped candidate app vibesys-run-g1c2" in line for line in logs)


def test_modal_teardown_deployment_is_best_effort_on_nonzero(monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    env = build_run_environment(RunEnvironmentSpec("modal"))
    logs = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001  # tracked: #288
        result = MagicMock()
        result.returncode = 1
        result.stderr = "boom"
        return result

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    # Must not raise.
    env.teardown_deployment("vibesys-run-g1c2", log=logs.append)
    assert any("failed" in line for line in logs)


def test_modal_teardown_deployment_is_best_effort_on_exception(monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    env = build_run_environment(RunEnvironmentSpec("modal"))
    logs = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001  # tracked: #288
        raise TimeoutError("stuck")

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    env.teardown_deployment("vibesys-run-g1c2", log=logs.append)
    assert any("raised" in line for line in logs)


@pytest.mark.parametrize("name", ["local", "docker"])
def test_non_modal_teardown_deployment_is_noop(name, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    env = build_run_environment(RunEnvironmentSpec(name))

    def fail_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001  # tracked: #288
        raise AssertionError("subprocess.run should not be called for non-Modal envs")  # noqa: TRY003  # tracked: #288

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fail_run)

    # No deployment to stop — must be a silent no-op.
    env.teardown_deployment("vibesys-run-g1c2", log=lambda _: None)


def test_modal_environment_prompt_notes_cover_seeded_checkouts(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """Seeded starting-point checkouts live only in the editor container, so
    the runtime notes must tell the agent to bake them into the Modal image;
    unseeded runs must not mention checkouts at all."""
    from vibesys.input_manifest import WorkspaceSource  # noqa: PLC0415  # tracked: #288

    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))
    source = WorkspaceSource(
        name="vllm",
        repo="https://github.com/vllm-project/vllm",
        commit="d7de043d55d1dd629554467e23874097e1c48993",
        dest="vllm",
    )

    seeded = env.open(
        _request(
            tmp_path,
            backend,
            agent_backend="cli",
            cli_provider="codex",
            workspace_sources=(source,),
        )
    )
    assert "seeded starting-point" not in seeded.view.prompt_notes.lower()
    notes = _modal_runtime_document(tmp_path)
    assert "`vllm/`" in notes
    assert "add_local_dir" in notes
    assert "copy=True" in notes

    unseeded_dir = tmp_path / "unseeded"
    unseeded_dir.mkdir()
    env.open(_request(unseeded_dir, backend, agent_backend="cli", cli_provider="codex"))
    unseeded_notes = _modal_runtime_document(unseeded_dir)
    assert "add_local_dir('vllm'" not in unseeded_notes
    assert "seeded starting-point" not in unseeded_notes.lower()


def test_docker_modal_and_skypilot_build_the_same_kind_of_sandbox_from_one_resource_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker, Modal, and SkyPilot all put the agent in a ``SandboxKind.DOCKER``
    editor container built from the same resource list (#679): only GPU-bound
    work dispatches remotely (the candidate's own ``modal run`` entrypoint, or
    a SkyPilot job), the agent itself never runs in a ``ModalSandbox`` or any
    other remote sandbox kind.
    """
    home = tmp_path / "synthetic-home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('{"synthetic": true}\n')
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(name="vibesys-evaluator-queue", version="0.1.0")
    )

    def _open(env_name: str, env: Any, backend: FakeBackend, **overrides: Any) -> None:  # noqa: ANN401  # tracked: #288
        root = tmp_path / env_name
        root.mkdir()
        env.open(
            _request(
                root,
                backend,
                objective="Optimize the service.\n",
                evaluator_package_root=package.root,
                agent_backend="cli",
                cli_provider="codex",
                **overrides,
            )
        )

    docker_backend = FakeBackend()
    _open("docker", build_run_environment(RunEnvironmentSpec("docker")), docker_backend)

    modal_backend = FakeBackend()
    _open("modal", build_run_environment(RunEnvironmentSpec("modal")), modal_backend)

    profiles = tmp_path / "clusters.toml"
    profiles.write_text(
        """schema_version = 1
[profiles.gpu]
runner = "skypilot"
infra = "slurm/example/gpu"
accelerator_backend = "rocm"
accelerator_type = "MI300A"
accelerators_per_node = 4
remote_artifact_root = "/remote/vibesys"
"""
    )

    class _FakeBridge:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401  # tracked: #288
            self.socket_path = kwargs["socket_path"]

        def start(self) -> None:
            self.socket_path.write_text("socket")

        def close(self) -> None:
            pass

    monkeypatch.setattr("vibesys.sandbox.run_environment.SkyPilotBridge", _FakeBridge)
    skypilot_backend = FakeBackend()
    skypilot_env = build_run_environment(
        make_run_environment_spec(
            use_skypilot=True,
            cluster_profile="gpu",
            cluster_profiles_file=profiles,
            resources=RunResourceRequest(
                nodes=1, accelerators_per_node=4, accelerator_backend="rocm"
            ),
        )
    )
    _open("skypilot", skypilot_env, skypilot_backend, state_namespace=MagicMock())

    backends = {"docker": docker_backend, "modal": modal_backend, "skypilot": skypilot_backend}

    # (a) One sandbox kind across all three environments.
    for env_name, backend in backends.items():
        assert backend.calls[0][0] is SandboxKind.DOCKER, env_name

    # (b) The common mount set declared by `_container_mount_plan` — the
    # objective doc, the evaluator package, and the CLI auth/framework
    # mounts — carries identical access and agent_path everywhere.
    resources_by_env = {
        env_name: {r.agent_path: r for r in backend.calls[0][1]["resources"]}
        for env_name, backend in backends.items()
    }
    common_agent_paths = {
        "/opt/vibesys-runtime/objective.md",
        "/opt/vibesys-evaluator-package",
        "/opt/vibesys",
        "/opt/vibesys-auth/0",
    }
    for agent_path in common_agent_paths:
        by_env = {env_name: mounts[agent_path] for env_name, mounts in resources_by_env.items()}
        accesses = {resource.access for resource in by_env.values()}
        assert accesses == {HostResourceAccess.READ_ONLY}, (agent_path, by_env)

    # (c) Nothing outside the workspace-related mounts each plan legitimately
    # marks writable (SkyPilot's caller-state bridge) is READ_WRITE.
    legitimately_writable = {
        "/opt/vibesys-skypilot/bridge.sock",
        "/opt/vibesys-skypilot/caller-state",
    }
    for env_name, mounts in resources_by_env.items():
        for agent_path, resource in mounts.items():
            if resource.access is HostResourceAccess.READ_WRITE:
                assert agent_path in legitimately_writable, (env_name, agent_path)
