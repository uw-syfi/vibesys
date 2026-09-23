"""Contract tests for the CI component selector."""

import shutil
import subprocess
from pathlib import Path

import pytest
from support.ci_impact import Component, SelectionError, changed_paths, load_policy, select
from support.ci_impact.cli import main
from support.ci_impact.model import _apply_edges, _native_jobs, _validate_native_targets
from support.ci_impact.selector import _native_languages

GIT = shutil.which("git") or "/usr/bin/git"


def test_cli_explain_and_validate(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "src/server/api/schema.py"]) == 0
    output = capsys.readouterr().out
    assert "Selected jobs: python, tui" in output
    assert "server_protocol" in output

    assert main(["validate"]) == 0
    assert "all tracked paths classified" in capsys.readouterr().out


def test_cli_plan_writes_actions_outputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    assert main(["plan", "--base", "HEAD", "--head", "HEAD", "--github-output", str(output)]) == 0
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["python"] == "false"
    assert values["native_targets"] == "[]"
    assert values["native_languages"] == "[]"


def test_native_owners_and_cross_component_effects() -> None:
    components, ignored_roots, ignored_files = load_policy()
    plan = select(["src/server/api/schema.py"], components, ignored_roots, ignored_files)
    assert plan["jobs"]["python"]
    assert plan["jobs"]["tui"]
    assert "python:server.api" in plan["components"]
    assert "server_protocol" in plan["components"]
    assert "clients" in plan["components"]
    assert not plan["jobs"]["evaluators"]


def test_native_job_only_for_registered_evaluator_roots() -> None:
    components, ignored_roots, ignored_files = load_policy()
    included = select(
        ["resources/evaluators/queue/go.mod"], components, ignored_roots, ignored_files
    )
    excluded = select(
        ["examples/microservices/hotel-correctness/.vibesys/tasks/compose/evaluator/go.mod"],
        components,
        ignored_roots,
        ignored_files,
    )
    assert included["jobs"]["evaluators"]
    assert not excluded["jobs"]["evaluators"]
    assert excluded["jobs"]["examples"]


def test_pnpm_workspace_dependents_are_selected() -> None:
    components, ignored_roots, ignored_files = load_policy()
    plan = select(["clients/backend-client/src/index.ts"], components, ignored_roots, ignored_files)
    assert plan["pnpm_packages"] == [
        "@vibesys/backend-client",
        "@vibesys/core-state",
        "@vibesys/tui",
    ]


def test_sdk_change_reaches_both_go_evaluators() -> None:
    components, ignored_roots, ignored_files = load_policy()
    plan = select(["sdk/vs-evaluator/vseval/go.mod"], components, ignored_roots, ignored_files)
    assert plan["native_targets"] == [
        "resources/evaluators/microservice",
        "resources/evaluators/queue",
        "sdk/vs-evaluator/vseval",
    ]
    assert plan["native_languages"] == ["go"]


def test_native_runner_change_reaches_its_go_evaluator() -> None:
    components, ignored_roots, ignored_files = load_policy()
    plan = select(
        ["resources/evaluators/queue/native_runner/Cargo.toml"],
        components,
        ignored_roots,
        ignored_files,
    )
    assert plan["native_targets"] == [
        "resources/evaluators/queue",
        "resources/evaluators/queue/native_runner",
    ]
    assert plan["native_languages"] == ["go", "rust"]


def test_native_languages_follow_new_manifest_roots(tmp_path: Path) -> None:
    go_root = tmp_path / "new-go"
    rust_root = tmp_path / "new-rust"
    go_root.mkdir()
    rust_root.mkdir()
    (go_root / "go.mod").write_text("module example.com/new-go\n", encoding="utf-8")
    (rust_root / "Cargo.toml").write_text("[package]\nname = 'new-rust'\n", encoding="utf-8")
    assert _native_languages(["new-go", "new-rust"], tmp_path) == ["go", "rust"]
    with pytest.raises(SelectionError, match=r"has no Cargo\.toml or go\.mod"):
        _native_languages(["unmanifested"], tmp_path)


def test_new_evaluator_manifest_requires_registered_target() -> None:
    with pytest.raises(SelectionError, match="has no registered CI target"):
        _native_jobs(
            "resources/evaluators/new-go-package",
            ("resources/evaluators/queue",),
            ("resources/evaluators",),
        )


def test_nested_native_manifest_must_reach_checked_target() -> None:
    components = {
        "native:checked": Component("native:checked", ("checked",), (), (), ("evaluators",), None),
        "native:checked/new": Component("native:checked/new", ("checked/new",), (), (), (), None),
    }
    with pytest.raises(SelectionError, match="does not reach a registered CI target"):
        _validate_native_targets(components, ("checked",), ())
    components["native:checked"] = Component(
        "native:checked", ("checked",), (), ("native:checked/new",), ("evaluators",), None
    )
    _validate_native_targets(components, ("checked",), ())


def test_malformed_policy_reports_missing_keys(tmp_path: Path) -> None:
    policy = tmp_path / "ci-components.toml"
    policy.write_text("components = []\n", encoding="utf-8")
    with pytest.raises(SelectionError, match="missing keys"):
        load_policy(policy)


def test_malformed_edge_reports_invalid_endpoint() -> None:
    components = {"a": Component("a", (), (), (), (), None)}
    with pytest.raises(SelectionError, match="edge endpoints must be component ids"):
        _apply_edges(components, [{"from": ["a"], "to": "a"}])


def test_global_ci_change_selects_all_registered_native_targets() -> None:
    components, ignored_roots, ignored_files = load_policy()
    plan = select(["ci-components.toml"], components, ignored_roots, ignored_files)
    assert plan["jobs"]["evaluators"]
    assert plan["jobs"]["go_prototype"]
    assert len(plan["native_targets"]) == 8


def test_go_prototype_change_only_selects_its_job() -> None:
    components, ignored_roots, ignored_files = load_policy()
    plan = select(["support/ci_impact_go/main.go"], components, ignored_roots, ignored_files)
    assert plan["jobs"]["go_prototype"]
    assert not plan["jobs"]["evaluators"]


def test_all_tracked_paths_are_classified() -> None:
    components, ignored_roots, ignored_files = load_policy()
    paths = subprocess.check_output([GIT, "ls-files", "-z"], text=True).split("\0")[:-1]  # noqa: S603
    select(paths, components, ignored_roots, ignored_files)


def test_reverse_dependency_closure_and_unknown_path() -> None:
    components = {
        "a": Component("a", (), ("a.py",), (), (), None),
        "b": Component("b", (), (), ("a",), (), None),
        "c": Component("c", (), (), ("b",), ("python",), None),
    }
    plan = select(["a.py"], components, (), ())
    assert plan["jobs"]["python"]
    assert "depends on b" in plan["components"]["c"][0]
    with pytest.raises(SelectionError, match="unowned changed paths"):
        select(["new_area/file.py"], components, (), ())


def test_changed_paths_include_both_sides_of_rename(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(  # noqa: S603
            [GIT, *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    (tmp_path / "before.py").write_text("content\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "before.py").rename(tmp_path / "after.py")
    git("add", "-A")
    git("commit", "-qm", "rename")
    head = git("rev-parse", "HEAD")
    assert changed_paths(base, head, "push", tmp_path) == ["before.py", "after.py"]


def test_pr_uses_merge_base(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(  # noqa: S603
            [GIT, *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    (tmp_path / "base").write_text("base")
    git("add", ".")
    git("commit", "-qm", "base")
    git("branch", "feature")
    (tmp_path / "main_only").write_text("main")
    git("add", ".")
    git("commit", "-qm", "main")
    main = git("rev-parse", "HEAD")
    git("checkout", "-q", "feature")
    (tmp_path / "feature_only").write_text("feature")
    git("add", ".")
    git("commit", "-qm", "feature")
    head = git("rev-parse", "HEAD")
    assert changed_paths(main, head, "pull_request", tmp_path) == ["feature_only"]
    assert set(changed_paths(main, head, "push", tmp_path)) == {"main_only", "feature_only"}


def test_initial_push_uses_root_commit(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(  # noqa: S603
            [GIT, *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    (tmp_path / "new.py").write_text("content\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    assert changed_paths("0" * 40, git("rev-parse", "HEAD"), "push", tmp_path) == ["new.py"]
