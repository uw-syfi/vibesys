from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import agentshim
import pytest
from tests.support import provider_profiles as fake_profiles

import vibesys
from vibesys.agents import host_resource_declarations
from vs_sandbox import HostResource, HostResourceAccess, HostResourceContext

_SHIPPED = ("claude", "codex", "gemini", "opencode")

# Stand-in profiles for the tests whose subject is VibeSys's declaration table
# rather than any CLI's declared state layout. agentshim registers real
# profiles for all four (and for providers VibeSys does not ship), but a test
# of the table should fail when the table changes, not when a library release
# moves one CLI's state directory. `TestShippedProfileState` covers the real
# profiles.
_FAKE_PROFILES = {
    "claude": fake_profiles.profile(
        "claude",
        state_dirs=(".claude", ".claude.json", ".config/claude"),
        darwin_state_dirs=(
            "Library/Application Support/claude",
            "Library/Caches/claude",
        ),
        state_root_env="CLAUDE_CONFIG_DIR",
    ),
    "codex": fake_profiles.profile(
        "codex",
        state_dirs=(".codex", ".config/codex"),
        darwin_state_dirs=(
            "Library/Application Support/codex",
            "Library/Application Support/com.openai.codex",
            "Library/Caches/codex",
        ),
        state_root_env="CODEX_HOME",
    ),
    "gemini": fake_profiles.profile("gemini", state_dirs=(".gemini", ".config/gemini")),
    "opencode": fake_profiles.profile(
        "opencode",
        state_dirs=(".local/share/opencode", ".config/opencode"),
    ),
}


class TestInstallRoot:
    """Agent packages may need binaries from sibling installation paths."""

    def test_node_package_imports_whole_package_tree(self):  # noqa: ANN201  # tracked: #288
        launcher = Path(
            "/home/u/.nvm/versions/node/v24/lib/node_modules/@openai/codex/bin/codex.js"
        )
        root = host_resource_declarations._install_root(launcher)  # noqa: SLF001  # tracked: #288

        assert root == Path("/home/u/.nvm/versions/node/v24/lib")
        platform_bin = Path(
            "/home/u/.nvm/versions/node/v24/lib/node_modules/@openai/"
            "codex/node_modules/@openai/codex-linux-x64/bin/codex"
        )
        assert platform_bin.is_relative_to(root)

    def test_plain_binary_imports_its_directory(self):  # noqa: ANN201  # tracked: #288
        assert host_resource_declarations._install_root(Path("/opt/tool/bin/agent")) == Path(  # noqa: SLF001  # tracked: #288
            "/opt/tool/bin"
        )


class TestInterpreterAliasRoots:
    """A venv reached through an alias directory must import that alias."""

    def test_alias_directory_between_venv_and_install_is_declared(self, monkeypatch, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        install = tmp_path / "cpython-3.14.7"
        (install / "bin").mkdir(parents=True)
        real = install / "bin" / "python3.14"
        real.write_text("#!/bin/false\n")
        alias = tmp_path / "cpython-3.14"
        alias.symlink_to(install)
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(alias / "bin" / "python3.14")
        (venv_bin / "python3").symlink_to("python")
        monkeypatch.setattr(host_resource_declarations.sys, "executable", str(venv_bin / "python3"))

        roots = host_resource_declarations._interpreter_alias_roots()  # noqa: SLF001  # tracked: #288

        # The alias, not the resolved install: sys.base_prefix already covers
        # the resolved path, and only the alias name dangles in the sandbox.
        assert roots == {alias}

    def test_no_alias_declares_nothing_extra(self, monkeypatch, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        real = tmp_path / "usr" / "bin" / "python3.14"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/false\n")
        monkeypatch.setattr(host_resource_declarations.sys, "executable", str(real))

        assert host_resource_declarations._interpreter_alias_roots() == set()  # noqa: SLF001  # tracked: #288


class TestAgentRuntime:
    """The running VibeSys install must be importable inside confinement.

    ``import vibesys`` cannot fail here: this test module is itself reached
    through ``vibesys.agents.host_resource_declarations``, so the package is
    already loaded and ``vibesys/__init__.py`` is a docstring-only module with
    no re-exports that could raise. The declaration is unconditional.
    """

    def test_declares_the_running_vibesys_package_root(self) -> None:
        declarations = tuple(
            host_resource_declarations._agent_runtime(  # noqa: SLF001  # tracked: #288
                HostResourceContext(env={})
            )
        )

        vibesys_root = Path(vibesys.__file__).resolve().parents[1]
        matching = [resource for resource in declarations if resource.path == vibesys_root]
        assert len(matching) == 1
        assert matching[0].access is HostResourceAccess.READ_ONLY
        assert matching[0].purpose == "agent and VibeSys runtime"


def test_defaults_declare_path_rust_and_shell_resources(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    home = tmp_path / "home"
    tool_bin = home / "tools" / "bin"
    cargo_bin = home / ".cargo" / "bin"
    rustup_home = home / ".rustup"
    bash_profile = home / ".bash_profile"

    declarations = host_resource_declarations.declare_agent_host_resources(
        {"HOME": str(home), "PATH": f"{tool_bin}:/usr/bin"},
        binary_path=None,
        provider="codex",
    )
    resources = {resource.path: resource.access for resource in declarations}

    assert resources[tool_bin] is HostResourceAccess.READ_ONLY
    assert resources[cargo_bin] is HostResourceAccess.READ_ONLY
    assert resources[rustup_home] is HostResourceAccess.READ_ONLY
    assert resources[bash_profile] is HostResourceAccess.READ_ONLY
    assert home not in resources


def test_active_rust_toolchain_declaration_is_narrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    rustup_home = home / ".rustup"
    sysroot = rustup_home / "toolchains" / "stable"
    target_libdir = sysroot / "lib" / "rustlib" / "host" / "lib"
    lib_dir = sysroot / "lib"
    lib_dir.mkdir(parents=True)
    target_libdir.mkdir(parents=True)
    monkeypatch.setattr(
        host_resource_declarations.shutil, "which", lambda *_args, **_kwargs: "rustc"
    )
    outputs = iter((f"{sysroot}\n", f"{target_libdir}\n"))
    monkeypatch.setattr(
        host_resource_declarations.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=next(outputs)),
    )

    declarations = tuple(
        host_resource_declarations.declare_active_rust_toolchain_resources(
            host_resource_declarations.HostResourceContext(
                env={"HOME": str(home), "PATH": str(home / ".cargo" / "bin")}
            )
        )
    )
    paths = {resource.path for resource in declarations}

    assert sysroot / "bin" in paths
    assert sysroot / "lib" in paths
    assert rustup_home not in paths


def _writable_state(
    tmp_path: Path,
    provider: str,
    env: dict[str, str] | None = None,
) -> set[str]:
    declarations = host_resource_declarations.declare_agent_host_resources(
        {"HOME": str(tmp_path), **(env or {})},
        binary_path=None,
        provider=provider,
    )
    return {
        resource.path.relative_to(tmp_path).as_posix()
        for resource in declarations
        if resource.access is HostResourceAccess.READ_WRITE
        and resource.path.is_relative_to(tmp_path)
    }


@pytest.fixture
def _fake_profiles_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every profile lookup from the fakes above, not from agentshim."""
    fake_profiles.install(monkeypatch, _FAKE_PROFILES)


@pytest.mark.parametrize(
    ("provider", "expected", "forbidden"),
    [
        ("codex", ".codex/auth.json", ".claude"),
        ("claude", ".claude", ".gemini"),
        ("gemini", ".gemini", ".config/opencode"),
        ("opencode", ".config/opencode", ".codex/auth.json"),
    ],
)
def test_provider_state_is_scoped_to_selected_agent(  # noqa: ANN201
    tmp_path,  # noqa: ANN001
    provider,  # noqa: ANN001
    expected,  # noqa: ANN001
    forbidden,  # noqa: ANN001
    _fake_profiles_installed,  # noqa: ANN001, PT019
):
    writable = _writable_state(tmp_path, provider)

    assert expected in writable
    assert forbidden not in writable


def test_codex_state_is_declared_as_leaf_files_not_the_whole_home(  # noqa: ANN201
    tmp_path,  # noqa: ANN001
    _fake_profiles_installed,  # noqa: ANN001, PT019
):
    writable = _writable_state(tmp_path, "codex")

    # A Codex checkout may live under $CODEX_HOME/worktrees, so the directory
    # itself must never be granted (#185). ``sessions`` is granted because a
    # rollout that does not outlive its turn makes every resume fail.
    assert writable == {
        ".codex/auth.json",
        ".codex/config.toml",
        ".codex/sessions",
        ".config/codex",
    }


def test_codex_home_relocates_the_state_leaves(tmp_path, _fake_profiles_installed):  # noqa: ANN001, ANN201, PT019
    relocated = tmp_path / "relocated-codex"

    writable = _writable_state(tmp_path, "codex", {"CODEX_HOME": str(relocated)})

    assert "relocated-codex/auth.json" in writable
    assert "relocated-codex/config.toml" in writable
    assert "relocated-codex/sessions" in writable
    assert ".codex/auth.json" not in writable
    # $CODEX_HOME does not move the XDG config directory.
    assert ".config/codex" in writable


def test_claude_config_dir_relocates_the_state_root(tmp_path, _fake_profiles_installed):  # noqa: ANN001, ANN201, PT019
    """A second provider with a state root variable gets the same generic rule.

    Claude declares no narrowed leaves, so its whole relocated directory is
    granted, unlike Codex's leaf-only grant.
    """
    relocated = tmp_path / "relocated-claude"

    writable = _writable_state(tmp_path, "claude", {"CLAUDE_CONFIG_DIR": str(relocated)})

    assert "relocated-claude" in writable
    assert ".claude" not in writable
    # $CLAUDE_CONFIG_DIR only relocates the first state directory.
    assert ".claude.json" in writable
    assert ".config/claude" in writable


def test_a_provider_agentshim_does_not_register_is_rejected(tmp_path):  # noqa: ANN001, ANN201
    with pytest.raises(ValueError, match="unregistered-provider"):
        _writable_state(tmp_path, "unregistered-provider")


class TestShippedProfileState:
    """The real agentshim profiles, run through the declaration table.

    Nothing here is monkeypatched: these pin that what the four shipped
    providers actually declare still satisfies what VibeSys derives from it, so
    a library release that renames or relocates a state directory fails here
    rather than confining an agent away from its own credentials.
    """

    @staticmethod
    def _declarations(tmp_path: Path, provider: str) -> tuple[HostResource, ...]:
        return tuple(
            host_resource_declarations._provider_state(  # noqa: SLF001
                host_resource_declarations.HostResourceContext(
                    env={"HOME": str(tmp_path)},
                    provider=provider,
                )
            )
        )

    @pytest.mark.parametrize("provider", _SHIPPED)
    def test_every_declared_state_directory_is_granted(self, tmp_path: Path, provider: str) -> None:
        declarations = self._declarations(tmp_path, provider)
        granted = {resource.path for resource in declarations}

        assert all(resource.access is HostResourceAccess.READ_WRITE for resource in declarations), (
            declarations
        )
        for state_dir in agentshim.get_provider(provider).profile.state_dirs:
            root = tmp_path / state_dir
            assert any(path == root or path.is_relative_to(root) for path in granted), state_dir

    def test_codex_state_stays_on_the_named_leaves(self, tmp_path: Path) -> None:
        granted = {resource.path for resource in self._declarations(tmp_path, "codex")}
        codex_home = tmp_path / ".codex"

        # A Codex checkout may live under $CODEX_HOME/worktrees, so the profile
        # naming the directory must still not widen the grant to it (#185).
        assert codex_home not in granted
        assert {path.name for path in granted if path.is_relative_to(codex_home)} == {
            "auth.json",
            "config.toml",
            "sessions",
        }


class TestContainerRuntimeResources:
    """Microservice candidates are container topologies the agent must drive."""

    def test_docker_socket_is_declared_writable(self):  # noqa: ANN201  # tracked: #288
        declarations = host_resource_declarations.container_runtime_resources({})

        writable = {
            resource.path
            for resource in declarations
            if resource.access is HostResourceAccess.READ_WRITE
        }
        assert Path("/var/run/docker.sock") in writable

    def test_custom_unix_docker_host_is_declared(self):  # noqa: ANN201  # tracked: #288
        declarations = host_resource_declarations.container_runtime_resources(
            {"DOCKER_HOST": "unix:///run/user/1000/docker.sock"}
        )

        paths = {resource.path for resource in declarations}
        assert Path("/run/user/1000/docker.sock") in paths

    def test_tcp_docker_host_declares_no_extra_path(self):  # noqa: ANN201  # tracked: #288
        declarations = host_resource_declarations.container_runtime_resources(
            {"DOCKER_HOST": "tcp://127.0.0.1:2375"}
        )

        assert {resource.path for resource in declarations} == {Path("/var/run/docker.sock")}


class TestTaskScratchDir:
    """Container bind sources resolve in the daemon's namespace, not the agent's.

    The scratch path therefore has to name the same directory inside and
    outside confinement, so it is a fixed host path rather than anything
    derived from the sandbox's private ``/tmp``.
    """

    def test_scratch_dir_follows_the_task_naming_convention(self):  # noqa: ANN201  # tracked: #288
        assert host_resource_declarations.task_scratch_dir("hotel-reservation") == Path(
            "/tmp/vibesys-hotel-reservation"  # noqa: S108  # tracked: #288
        )


class TestTaskAgentHostResources:
    """Reaching the Docker socket is root-equivalent, so the widening is scoped."""

    @pytest.fixture(autouse=True)
    def _scratch_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the declaration's mkdir side effect inside the test's tmp dir."""
        monkeypatch.setattr(host_resource_declarations, "TASK_SCRATCH_ROOT", tmp_path)

    def test_container_topology_declares_socket_and_scratch(self, tmp_path: Path) -> None:
        declarations = host_resource_declarations.task_agent_host_resources(
            container_topology=True,
            cli_sandboxed=False,
            task_name="hotel-reservation",
            evaluator_package_root=None,
            env={},
        )

        scratch = tmp_path / "vibesys-hotel-reservation"
        resources = {resource.path: resource.access for resource in declarations}
        assert resources[Path("/var/run/docker.sock")] is HostResourceAccess.READ_WRITE
        assert resources[scratch] is HostResourceAccess.READ_WRITE
        # The benchmark writes captures here, so it must exist before the run.
        assert scratch.is_dir()

    def test_other_domains_declare_nothing_extra(self) -> None:
        assert (
            host_resource_declarations.task_agent_host_resources(
                container_topology=False,
                cli_sandboxed=False,
                task_name="latency",
                evaluator_package_root=None,
                env={},
            )
            == ()
        )

    def test_container_backend_owns_its_own_exposure(self, tmp_path: Path) -> None:
        assert (
            host_resource_declarations.task_agent_host_resources(
                container_topology=True,
                cli_sandboxed=True,
                task_name="hotel-reservation",
                evaluator_package_root=tmp_path / "evaluator",
                env={},
            )
            == ()
        )

    def test_scratch_is_skipped_without_a_named_task(self) -> None:
        declarations = host_resource_declarations.task_agent_host_resources(
            container_topology=True,
            cli_sandboxed=False,
            task_name=None,
            evaluator_package_root=None,
            env={},
        )

        assert {resource.path for resource in declarations} == {Path("/var/run/docker.sock")}

    def test_evaluator_package_is_read_only_and_domain_independent(self, tmp_path: Path) -> None:
        package_root = tmp_path / "evaluator"
        tools_root = tmp_path / "operator-tools" / "request-factory" / "digest"
        declarations = host_resource_declarations.task_agent_host_resources(
            container_topology=False,
            cli_sandboxed=False,
            task_name=None,
            evaluator_package_root=package_root,
            evaluator_tool_roots=(tools_root,),
            env={},
        )

        # Read-only: the evaluator is trusted, integrity-checked input no role
        # may edit.
        assert {resource.path: resource.access for resource in declarations} == {
            package_root: HostResourceAccess.READ_ONLY,
            tools_root: HostResourceAccess.READ_ONLY,
        }
