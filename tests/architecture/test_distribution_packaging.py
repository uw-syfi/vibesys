"""Contracts for the Python packages owned by the ``vibesys`` distribution."""

from __future__ import annotations

import configparser
import importlib.util
import re
import shlex
import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.requirements import Requirement
from scripts.example_repositories import is_example_repository_path

if TYPE_CHECKING:
    from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTERNAL_DISTRIBUTIONS = {
    "vs-correctness",
    "vs-evaluator-protocol",
    "vs-feature-flags",
    "vs-github",
    "vs-issue-board",
    "vs-loop-state",
    "vs-project",
    "vs-prompts",
    "vs-sandbox",
}
INTERNAL_IMPORT_PACKAGES = {
    "vs_correctness",
    "vs_evaluator_protocol",
    "vs_feature_flags",
    "vs_github",
    "vs_issue_board",
    "vs_loop_state",
    "vs_project",
    "vs_prompts",
    "vs_sandbox",
}


def test_headless_readme_examples_select_a_run_collection() -> None:
    """Removing the explicit collection from a headless example must fail here."""
    result = subprocess.run(
        ["git", "ls-files", "--", "examples/**/README.md"],  # noqa: S607
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
    # examples' task overlays.
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
    result = subprocess.run(
        [  # noqa: S607
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
    module_path = PROJECT_ROOT / "packaging_support.py"
    assert module_path.is_file()
    spec = importlib.util.spec_from_file_location("packaging_support", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_distribution_discovers_internal_packages_from_their_source_roots():  # noqa: ANN201
    """Dropping one source root must make its import package disappear from the wheel."""
    module = _load_packaging_support()
    packages, package_dirs = module.discover_distribution_packages(PROJECT_ROOT)

    assert {"entrypoints", "server", "vibesys", *INTERNAL_IMPORT_PACKAGES} <= set(packages)
    assert "vibesys.prompts.backend.cuda" in packages
    assert package_dirs["vibesys"] == "src/vibesys"
    assert package_dirs["entrypoints"] == "src/entrypoints"
    assert package_dirs["server"] == "src/server"
    assert package_dirs["vs_feature_flags"] == ("libs/vs-feature-flags/src/vs_feature_flags")
    assert package_dirs["vs_correctness"] == "libs/vs-correctness/src/vs_correctness"
    assert package_dirs["vs_prompts"] == "libs/vs-prompts/src/vs_prompts"
    assert package_dirs["vs_sandbox"] == "libs/vs-sandbox/src/vs_sandbox"


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


def test_root_metadata_declares_internal_runtime_dependencies_directly():  # noqa: ANN201
    """Reintroducing a workspace-only dependency must make PyPI resolution fail here."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    requirements = {Requirement(raw).name for raw in pyproject["project"]["dependencies"]}

    assert requirements.isdisjoint(INTERNAL_DISTRIBUTIONS)
    assert {"mcp", "modal"} <= requirements
    assert pyproject["tool"]["uv"]["workspace"]["members"] == ["sdk/*"]


def test_frontend_payload_belongs_to_the_entrypoints_package() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert "_tui/**/*" in package_data["entrypoints"]
    assert "_tui/**/*" not in package_data["vibesys"]


def test_built_distribution_caps_dependencies_without_current_intel_macos_wheels(
    tmp_path: Path,
) -> None:
    """Published metadata must preserve constraints needed by Intel macOS installs."""
    subprocess.run(  # noqa: S603
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],  # noqa: S607
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("vibesys-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert not any(Path(name).name == "agent.toml" for name in archive.namelist())
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


def test_sdist_contains_evaluator_packages_without_local_build_outputs(tmp_path: Path) -> None:
    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text().splitlines()
    assert "graft resources/evaluators" in manifest
    assert "prune resources/evaluators/queue/native_runner/target" in manifest

    subprocess.run(  # noqa: S603
        ["uv", "build", "--sdist", "--out-dir", str(tmp_path)],  # noqa: S607
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sdist = next(tmp_path.glob("vibesys-*.tar.gz"))
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = {member.name.partition("/")[2] for member in archive.getmembers()}

    assert "resources/evaluators/queue/vibesys.evaluator.toml" in members
    assert "resources/evaluators/microservice/vibesys.evaluator.toml" in members
    assert "clients/backend-client/src/index.ts" in members
    assert "clients/core-state/src/index.ts" in members
    assert "clients/tui/src/index.ts" in members
    assert "clients/backend-client/package.json" in members
    assert "clients/core-state/package.json" in members
    assert "clients/tui/package.json" in members
    assert "pnpm-lock.yaml" in members
    assert "tsconfig.architecture.json" in members
    assert ".dependency-cruiser.cjs" in members
    assert "scripts/check_ts_architecture.test.mjs" in members
    assert "scripts/check_ts_package_manifests.mjs" in members
    assert not any(
        member.startswith("resources/evaluators/") and "/target/" in member for member in members
    )
