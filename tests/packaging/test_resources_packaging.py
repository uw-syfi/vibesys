"""Tests for wheel-time resource staging and run-time resource resolution."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  # tracked: #288

import pytest

# `resources_packaging` is a build-time module in `packaging/` (imported by
# setup.py). It is not part of the installed package; pytest picks it up via
# `pythonpath = [".", "packaging"]`, and the type checker via `[tool.ty.environment] root`.
from resources_packaging import (
    PackagingError,
    stage_resources,
    stage_sdk,
)

from vibesys import resource_paths
from vibesys.constants import PROJECT_ROOT


def _make_fake_repo(root: Path) -> Path:
    (root / "resources" / "profilers" / "nsys").mkdir(parents=True)
    (root / "resources" / "profilers" / "nsys" / "server.py").write_text("# server")
    (root / "resources" / "profilers" / "nsys" / "__pycache__").mkdir()
    (root / "resources" / "profilers" / "nsys" / "__pycache__" / "server.pyc").write_text("x")
    common = root / "resources" / "profilers" / "_common"
    common.mkdir(parents=True)
    (common / "capture_runtime.py").write_text("# capture runtime")
    evaluator = root / "resources" / "evaluators" / "queue"
    evaluator.mkdir(parents=True)
    (evaluator / "vibesys.evaluator.toml").write_text("schema_version = 1\n")
    (evaluator / "native_runner" / "target" / "debug").mkdir(parents=True)
    (evaluator / "native_runner" / "target" / "debug" / "runner").write_text("build output")
    skill = root / "resources" / "skills" / "serving-systems"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: serving-systems\ndescription: d\n---\n")
    (skill / ".vibesys.toml").write_text('[[rule]]\npath = "."\n')
    (skill / "repos" / "vllm").mkdir(parents=True)
    (skill / "repos" / "vllm" / "big.bin").write_text("vendored checkout")
    return root


def test_stage_resources_copies_trees_and_drops_vendored_checkouts(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    repo = _make_fake_repo(tmp_path / "repo")
    dest = tmp_path / "build" / "vibesys" / "_resources"

    assert stage_resources(repo, dest)
    assert (dest / "profilers" / "nsys" / "server.py").is_file()
    assert (dest / "profilers" / "_common" / "capture_runtime.py").is_file()
    assert (dest / "skills" / "serving-systems" / "SKILL.md").is_file()
    assert (dest / "skills" / "serving-systems" / ".vibesys.toml").is_file()
    assert (dest / "evaluators" / "queue" / "vibesys.evaluator.toml").is_file()
    assert not (dest / "evaluators" / "queue" / "native_runner" / "target").exists()
    assert not (dest / "skills" / "serving-systems" / "repos").exists()
    assert not (dest / "profilers" / "nsys" / "__pycache__").exists()


def test_stage_resources_is_idempotent(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    repo = _make_fake_repo(tmp_path / "repo")
    dest = tmp_path / "dest"

    assert stage_resources(repo, dest)
    assert stage_resources(repo, dest)
    assert (dest / "profilers" / "nsys" / "server.py").is_file()


def test_stage_resources_without_resources_dir_is_a_noop(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    dest = tmp_path / "dest"

    assert not stage_resources(tmp_path / "empty-repo", dest)
    assert not dest.exists()


def test_required_resource_staging_rejects_a_missing_tree(tmp_path):  # noqa: ANN001, ANN201
    repo = tmp_path / "repo"
    (repo / "resources" / "profilers").mkdir(parents=True)

    with pytest.raises(PackagingError, match="resources/skills"):
        stage_resources(repo, tmp_path / "dest", required=True)


def test_stage_sdk_copies_only_the_installable_project(tmp_path):  # noqa: ANN001, ANN201
    repo = tmp_path / "repo"
    sdk = repo / "sdk" / "vs-bench"
    (sdk / "src" / "vs_bench" / "__pycache__").mkdir(parents=True)
    (sdk / "tests").mkdir()
    (sdk / "pyproject.toml").write_text("[project]\nname = 'vs-bench'\n")
    (sdk / "README.md").write_text("# vs-bench\n")
    (sdk / "src" / "vs_bench" / "__init__.py").write_text("")
    (sdk / "src" / "vs_bench" / "py.typed").write_text("")
    (sdk / "src" / "vs_bench" / "__pycache__" / "module.pyc").write_text("cache")
    (sdk / "tests" / "test_sdk.py").write_text("assert True\n")
    dest = tmp_path / "build" / "vibesys" / "_sdk"

    assert stage_sdk(repo, dest, required=True)
    assert (dest / "vs-bench" / "pyproject.toml").is_file()
    assert (dest / "vs-bench" / "README.md").is_file()
    assert (dest / "vs-bench" / "src" / "vs_bench" / "py.typed").is_file()
    assert not (dest / "vs-bench" / "tests").exists()
    assert not (dest / "vs-bench" / "src" / "vs_bench" / "__pycache__").exists()


def test_required_sdk_staging_rejects_an_incomplete_project(tmp_path):  # noqa: ANN001, ANN201
    sdk = tmp_path / "repo" / "sdk" / "vs-bench"
    sdk.mkdir(parents=True)
    (sdk / "pyproject.toml").write_text("[project]\nname = 'vs-bench'\n")

    with pytest.raises(PackagingError, match="sdk/vs-bench/src"):
        stage_sdk(tmp_path / "repo", tmp_path / "dest", required=True)


def test_resources_root_prefers_the_checkout():  # noqa: ANN201  # tracked: #288
    assert resource_paths.resources_root() == PROJECT_ROOT / "resources"


def test_profiler_support_dir_resolves_known_kind_and_rejects_unknown():  # noqa: ANN201  # tracked: #288
    nsys = resource_paths.profiler_support_dir("nsys")
    assert nsys is not None
    assert (nsys / "server.py").is_file()
    assert resource_paths.profiler_support_dir("no-such-profiler") is None


def test_profiler_support_common_dir_points_at_the_shared_capture_runtime():  # noqa: ANN201  # tracked: #288
    common = resource_paths.profiler_support_common_dir()
    assert common is not None
    assert (common / "capture_runtime.py").is_file()
    assert resource_paths.PROFILERS_COMMON_STAGED_NAME == "profilers_common"


def test_default_skill_roots_point_at_the_resources_tree():  # noqa: ANN201  # tracked: #288
    roots = resource_paths.default_skill_roots()
    assert roots == (PROJECT_ROOT / "resources" / "skills",)


def test_evaluator_packages_dir_points_at_the_resources_tree() -> None:
    assert resource_paths.evaluator_packages_dir() == PROJECT_ROOT / "resources" / "evaluators"


def test_resources_root_falls_back_to_the_staged_wheel_copy(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    fake_checkout = tmp_path / "no-checkout"
    fake_package = tmp_path / "site-packages" / "vibesys"
    staged = fake_package / "_resources"
    (staged / "evaluators").mkdir(parents=True)
    (staged / "profilers" / "nsys").mkdir(parents=True)
    (staged / "profilers" / "nsys" / "server.py").write_text("# server")
    (staged / "profilers" / "_common").mkdir(parents=True)
    (staged / "profilers" / "_common" / "capture_runtime.py").write_text("# capture runtime")
    (staged / "skills").mkdir()

    monkeypatch.setattr(resource_paths, "PROJECT_ROOT", fake_checkout)
    monkeypatch.setattr(resource_paths, "files", lambda _package: fake_package)

    assert resource_paths.resources_root() == staged
    assert resource_paths.evaluator_packages_dir() == staged / "evaluators"
    support = resource_paths.profiler_support_dir("nsys")
    assert support == staged / "profilers" / "nsys"
    common = resource_paths.profiler_support_common_dir()
    assert common == staged / "profilers" / "_common"
    assert resource_paths.default_skill_roots() == (staged / "skills",)


def test_resources_root_is_none_without_checkout_or_staged_copy(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    monkeypatch.setattr(resource_paths, "PROJECT_ROOT", tmp_path / "nowhere")
    monkeypatch.setattr(resource_paths, "files", lambda _package: tmp_path / "no-package")

    assert resource_paths.resources_root() is None
    assert resource_paths.profiler_support_dir("nsys") is None
    assert resource_paths.profiler_support_common_dir() is None
    assert resource_paths.default_skill_roots() == ()
