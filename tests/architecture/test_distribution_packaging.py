"""Contracts for the Python packages owned by the ``vibesys`` distribution."""

from __future__ import annotations

import configparser
import importlib.util
import re
import shlex
import sys
import sysconfig
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from scripts.example_repositories import is_example_repository_path
from tests.support import run_test_command

if TYPE_CHECKING:
    from types import ModuleType

    from tests.support.distribution_build import BuiltDistributions, BuiltWheel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTERNAL_DISTRIBUTIONS = frozenset(
    tomllib.loads(project.read_text())["project"]["name"]
    for project in (PROJECT_ROOT / "libs").glob("*/pyproject.toml")
)
INTERNAL_IMPORT_PACKAGES = {
    "vs_async_ops",
    "vs_evaluator_protocol",
    "vs_evaluation",
    "vs_github",
    "vs_issue_tracker",
    "vs_core",
    "vs_project",
    "vs_prompts",
    "vs_runtime",
    "vs_sandbox",
    "vs_slurm",
    "vs_mcp",
}


def test_headless_readme_examples_select_a_run_collection() -> None:
    """Removing the explicit collection from a headless example must fail here."""
    result = run_test_command(
        ["git", "ls-files", "--", "examples/**/README.md"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    readmes = [
        PROJECT_ROOT / relative_path
        for relative_path in result.stdout.splitlines()
        if (PROJECT_ROOT / relative_path).is_file()
        and "--headless" in (PROJECT_ROOT / relative_path).read_text()
        and "--input" in (PROJECT_ROOT / relative_path).read_text()
    ]
    assert readmes

    for readme in readmes:
        blocks = re.findall(r"```bash\n(.*?)```", readme.read_text(), flags=re.DOTALL)
        headless_block = next(block for block in blocks if "--headless" in block)
        arguments = shlex.split(headless_block.replace("\\\n", " "))
        runs_index = arguments.index("--runs-dir")
        assert arguments[runs_index + 1] == "/work/vibesys-runs", readme


def _submodule_configuration() -> tuple[configparser.ConfigParser, list[str]]:
    config = configparser.ConfigParser()
    config.read(PROJECT_ROOT / ".gitmodules")
    submodule_sections = [
        section for section in config.sections() if section.startswith('submodule "')
    ]
    assert submodule_sections
    return config, submodule_sections


def _is_repository_example(config: configparser.ConfigParser, section: str) -> bool:
    # One definition, shared with the CI fetcher that materializes these
    # examples' external repositories.
    return is_example_repository_path(Path(config.get(section, "path")))


def test_vcs_installs_do_not_initialize_supporting_submodules() -> None:
    """Tooling and reference submodules must remain opt-in for source installs."""
    config, submodule_sections = _submodule_configuration()
    supporting_sections = [
        section for section in submodule_sections if not _is_repository_example(config, section)
    ]

    assert [
        section
        for section in supporting_sections
        if config.get(section, "update", fallback=None) != "none"
    ] == []


def test_repository_examples_track_their_vibesys_branches() -> None:
    """Runnable repository examples must initialize with the authored task branch."""
    config, submodule_sections = _submodule_configuration()
    repository_sections = [
        section for section in submodule_sections if _is_repository_example(config, section)
    ]

    assert repository_sections
    for section in repository_sections:
        assert not config.has_option(section, "update")
        assert config.get(section, "branch") == "vibesys"
        assert config.getboolean(section, "shallow")
        assert config.get(section, "url").startswith("https://github.com/vibesys-playground/")


def test_tracked_submodule_initialization_commands_override_the_opt_out() -> None:
    """Adding an ineffective setup command must make the documentation contract fail."""
    result = run_test_command(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            "*.md",
            "*.sh",
            ":(exclude)third_party/**",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_files = [
        Path(path) for path in result.stdout.split("\0") if path and (PROJECT_ROOT / path).is_file()
    ]

    ineffective_commands = []
    for relative_path in tracked_files:
        for line_number, line in enumerate(
            (PROJECT_ROOT / relative_path).read_text().splitlines(), start=1
        ):
            if (
                re.search(r"\bgit\s+submodule\s+update\b", line)
                and "--init" in line
                and "--checkout" not in line
            ):
                ineffective_commands.append(f"{relative_path}:{line_number}")

    assert ineffective_commands == []


def _load_packaging_support() -> ModuleType:
    module_path = PROJECT_ROOT / "packaging" / "packaging_support.py"
    assert module_path.is_file()
    spec = importlib.util.spec_from_file_location("packaging_support", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_distribution_discovers_internal_packages_from_their_source_roots() -> None:
    """Dropping one source root must make its import package disappear from the wheel."""
    module = _load_packaging_support()
    packages, package_dirs = module.discover_distribution_packages(PROJECT_ROOT)

    assert {"entrypoints", "server", "vibesys", *INTERNAL_IMPORT_PACKAGES} <= set(packages)
    assert "vibesys.prompts.backend.cuda" in packages
    assert "vibesys.prompts" in packages
    assert "vibesys.domains" in packages
    assert package_dirs["vibesys"] == "src/vibesys"
    assert package_dirs["entrypoints"] == "src/entrypoints"
    assert package_dirs["server"] == "src/server"
    assert package_dirs["vs_prompts"] == "libs/vs-prompts/src/vs_prompts"
    assert package_dirs["vs_runtime"] == "libs/vs-runtime/src/vs_runtime"
    assert package_dirs["vs_sandbox"] == "libs/vs-sandbox/src/vs_sandbox"
    assert package_dirs["vs_async_ops"] == "libs/vs-async-ops/src/vs_async_ops"
    assert package_dirs["vs_evaluation"] == "libs/vs-evaluation/src/vs_evaluation"
    assert package_dirs["vs_slurm"] == "libs/vs-slurm/src/vs_slurm"


def test_namespace_discovery_excludes_build_and_cache_artifacts(tmp_path: Path) -> None:
    """Local interpreter and build artifacts must never become wheel packages."""
    module = _load_packaging_support()
    package = tmp_path / "src" / "example"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "namespace").mkdir()
    for artifact in (
        "__pycache__",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    ):
        (package / artifact / "nested").mkdir(parents=True)

    packages, package_dirs = module.discover_distribution_packages(tmp_path)

    assert packages == ["example", "example.namespace"]
    assert package_dirs == {
        "example": "src/example",
        "example.namespace": "src/example/namespace",
    }


def test_client_source_discovery_is_tracked_and_excludes_local_state(
    tmp_path: Path,
) -> None:
    module = _load_packaging_support()
    clients = tmp_path / "clients"
    (clients / "package.json").parent.mkdir(parents=True)
    (clients / "package.json").write_text("{}\n")
    script = clients / "tools" / "scripts" / "check.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("export {};\n")
    package = tmp_path / "clients" / "libs" / "example"
    source = package / "src" / "index.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const value = 1;\n")
    (package / "package.json").write_text("{}\n")
    (package / "dist").mkdir()
    (package / "dist" / "index.js").write_text("generated\n")
    (package / "node_modules" / "dependency").mkdir(parents=True)
    (package / "node_modules" / "dependency" / "index.js").write_text("installed\n")
    undeclared = tmp_path / "clients" / "undeclared" / "src"
    undeclared.mkdir(parents=True)
    (undeclared / "index.ts").write_text("export {};\n")

    tracked = [clients / "package.json", script, package / "package.json", source]
    run_test_command(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    run_test_command(
        ["git", "add", "--", *(str(path.relative_to(tmp_path)) for path in tracked)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    local_paths = [package / "dist" / "index.js", package / "node_modules/dependency/index.js"]
    for scope in (clients, package):
        (scope / ".env").write_text("TOKEN=secret\n")
        local_paths.append(scope / ".env")
        for local_directory in (".vibesys-demo", "artifacts", ".browser-dist"):
            output = scope / local_directory / "local.txt"
            output.parent.mkdir(parents=True)
            output.write_text("local\n")
            local_paths.append(output)
    run_test_command(
        [
            "git",
            "add",
            "--force",
            "--",
            *(str(path.relative_to(tmp_path)) for path in local_paths),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (package / "untracked-secret.txt").write_text("secret\n")

    sources = module.discover_client_workspace_sources(tmp_path)
    assert sources == [
        "clients/libs/example/package.json",
        "clients/libs/example/src/index.ts",
        "clients/package.json",
        "clients/tools/scripts/check.mjs",
    ]
    assert module.is_client_workspace_path("clients/libs/example/src/index.ts")
    assert not module.is_client_workspace_path("src/vibesys/__init__.py")
    for local_path in (
        "clients/.env",
        "clients/.vibesys-demo/local.txt",
        "clients/artifacts/local.txt",
        "clients/.browser-dist/local.txt",
        "clients/libs/example/.env",
        "clients/libs/example/.vibesys-demo/local.txt",
        "clients/libs/example/artifacts/local.txt",
        "clients/libs/example/.browser-dist/local.txt",
        "clients/libs/example/dist/index.js",
        "clients/libs/example/node_modules/dependency/index.js",
    ):
        assert local_path not in sources
    assert "clients/libs/example/untracked-secret.txt" not in sources


def test_client_source_discovery_without_git_uses_only_canonical_roots(tmp_path: Path) -> None:
    module = _load_packaging_support()
    package = tmp_path / "clients" / "libs" / "example"
    source = package / "src" / "index.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const value = 1;\n")
    (package / "package.json").write_text("{}\n")
    (package / ".env.local").write_text("TOKEN=secret\n")
    local_output = package / "artifacts" / "local.txt"
    local_output.parent.mkdir()
    local_output.write_text("local\n")
    undeclared = tmp_path / "clients" / "undeclared" / "src" / "index.ts"
    undeclared.parent.mkdir(parents=True)
    undeclared.write_text("export {};\n")

    assert module.discover_client_workspace_sources(tmp_path) == [
        "clients/libs/example/package.json",
        "clients/libs/example/src/index.ts",
    ]


def test_client_source_discovery_fails_closed_when_checkout_tracking_is_unreadable(
    tmp_path: Path,
) -> None:
    module = _load_packaging_support()
    package = tmp_path / "clients" / "libs" / "example"
    package.mkdir(parents=True)
    (package / "package.json").write_text("{}\n")
    (tmp_path / ".git").mkdir()

    with pytest.raises(module.ClientSourceDiscoveryError, match="Git-tracked client sources"):
        module.discover_client_workspace_sources(tmp_path)


def test_build_output_cleanup_removes_only_owned_top_level_packages(tmp_path: Path) -> None:
    module = _load_packaging_support()
    build_lib = tmp_path / "build"
    stale = build_lib / "example" / "deleted.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("STALE = True\n")
    unrelated = build_lib / "other" / "keep.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("KEEP = True\n")

    module.clear_distribution_build_outputs(build_lib, ["example", "example.nested"])

    assert not stale.parent.exists()
    assert unrelated.is_file()


def test_root_metadata_declares_internal_runtime_dependencies_directly() -> None:
    """Reintroducing a workspace-only dependency must make PyPI resolution fail here."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    requirements = {Requirement(raw).name for raw in pyproject["project"]["dependencies"]}

    assert requirements.isdisjoint(INTERNAL_DISTRIBUTIONS)
    assert {"mcp", "modal"} <= requirements
    assert pyproject["tool"]["uv"]["workspace"]["members"] == ["sdk/*", "libs/*"]
    libraries = sorted((PROJECT_ROOT / "libs").iterdir())
    assert libraries
    assert all((library / "pyproject.toml").is_file() for library in libraries)


@pytest.mark.xdist_group("built_wheel")
def test_built_wheel_imports_and_validates_proto_contracts(built_wheel: BuiltWheel) -> None:
    """Exercise the shipped package without editable source paths or site startup."""
    source = built_wheel.source.root / "libs/vs-proto/src"
    with zipfile.ZipFile(built_wheel.wheel) as archive:
        for module in (source / "vs_proto").rglob("*.py"):
            member = module.relative_to(source).as_posix()
            assert archive.read(member) == module.read_bytes(), member
    script = """
import sys
sys.path[:0] = sys.argv[1:3]
from vs_proto.api import ValidationError, replay, validate
validate(replay.ReplayPolicy(reply_type="test.Reply"))
try:
    validate(replay.ReplayPolicy())
except ValidationError:
    pass
else:
    raise AssertionError("installed validation accepted an invalid reply contract")
"""
    result = run_test_command(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(built_wheel.wheel),
            sysconfig.get_path("purelib"),
        ],
        cwd=built_wheel.wheel.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.xdist_group("built_wheel")
def test_built_distribution_caps_dependencies_without_current_intel_macos_wheels(
    built_wheel: BuiltWheel,
) -> None:
    """Published metadata must preserve constraints needed by Intel macOS installs."""
    distribution_source = built_wheel.source
    with zipfile.ZipFile(built_wheel.wheel) as archive:
        members = set(archive.namelist())
        assert not any(Path(name).name == "agent.toml" for name in members)
        assert not any(
            name.startswith(("vibesys/orchestration/prompts/", "vibesys/orchestration/domains/"))
            for name in members
        )
        source = distribution_source.root / "src"
        prompt_assets = {
            path.relative_to(source).as_posix(): path
            for path in (source / "vibesys").rglob("*")
            if path.is_file()
            and path.suffix in {".j2", ".md"}
            and "prompts" in path.relative_to(source).parts
        }
        assert prompt_assets.keys() <= members
        for member, path in prompt_assets.items():
            assert archive.read(member) == path.read_bytes(), member
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(archive.read(metadata_path).decode())

    requirements = {
        requirement.name: requirement
        for raw in metadata.get_all("Requires-Dist", [])
        if (requirement := Requirement(raw))
    }

    assert str(requirements["cbor2"].specifier) == "<6"
    assert str(requirements["cryptography"].specifier) == "<50"
    assert str(requirements["mcp"].specifier) == "<2,>=1.0"


@pytest.mark.xdist_group("built_wheel")
def test_sdist_contains_evaluator_packages_without_local_build_outputs(
    built_distributions: BuiltDistributions,
) -> None:
    source_root = built_distributions.source.root
    manifest = (source_root / "MANIFEST.in").read_text().splitlines()
    assert "graft resources/evaluators" in manifest
    assert "prune resources/evaluators/*/native_runner/target" in manifest
    assert "graft clients" not in manifest

    with tarfile.open(built_distributions.sdist, mode="r:gz") as archive:
        members = {
            member.name.partition("/")[2] for member in archive.getmembers() if member.isfile()
        }

    workspace_members = tomllib.loads((source_root / "pyproject.toml").read_text())["tool"]["uv"][
        "workspace"
    ]["members"]
    for member_glob in workspace_members:
        for manifest_path in source_root.glob(f"{member_glob}/pyproject.toml"):
            if member_glob.startswith("libs/"):
                assert manifest_path.relative_to(source_root).as_posix() in members

    assert "resources/evaluators/queue/vibesys.evaluator.toml" in members
    assert "resources/evaluators/microservice/vibesys.evaluator.toml" in members
    assert "resources/evaluators/microservice/kubernetes_runtime/cli.py" in members
    assert "resources/evaluators/microservice/kubernetes_runtime/control.py" in members
    assert "resources/evaluators/microservice/kubernetes_runtime/runtime.py" in members
    client_root = source_root / "clients"
    workspace_packages = sorted(
        manifest_path.parent
        for pattern in ("*/package.json", "libs/*/package.json")
        for manifest_path in client_root.glob(pattern)
        if manifest_path.parent.name not in {"bower_components", "node_modules"}
    )
    assert workspace_packages
    for package in workspace_packages:
        relative_package = package.relative_to(source_root).as_posix()
        assert f"{relative_package}/package.json" in members
        source_entry = package / "src" / "index.ts"
        if source_entry.is_file():
            assert source_entry.relative_to(source_root).as_posix() in members

    assert "clients/pnpm-lock.yaml" in members
    assert "clients/biome.warnings.json" in members
    assert "clients/tools/tsconfig.architecture.json" in members
    assert "clients/.dependency-cruiser.mjs" in members
    assert "clients/tools/scripts/check_ts_architecture.mjs" in members
    assert "clients/tools/scripts/check_ts_architecture.test.mjs" in members
    assert "clients/tools/scripts/check_package_graph.mjs" in members
    assert "clients/tools/scripts/workspace_layout.mjs" in members
    module = _load_packaging_support()
    expected_client_sources = set(module.discover_client_workspace_sources(source_root))
    assert {
        member for member in members if member.startswith("clients/")
    } == expected_client_sources
    assert not any(
        member.startswith("resources/evaluators/") and "/target/" in member for member in members
    )
    assert not any(
        member.startswith("clients/")
        and any(
            part
            in {
                ".browser-dist",
                ".vibesys-demo",
                "artifacts",
                "coverage",
                "dist",
                "node_modules",
                "playwright-report",
                "test-results",
            }
            for part in Path(member).parts
        )
        for member in members
    )
    assert not any(
        member.startswith("clients/") and Path(member).name.startswith(".env") for member in members
    )
