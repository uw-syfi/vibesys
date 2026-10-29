from datetime import UTC, datetime
from pathlib import Path

from vibesys.run.project_policy import build_project_path_policy
from vs_project.api import Project


def test_uninitialized_project_policy_protects_no_authored_inputs(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("OBJECTIVE.md", "vibesys.input.toml", "reference", "benchmark", "_evaluator"):
        path = project / name
        if path.suffix:
            path.write_text("candidate\n")
        else:
            path.mkdir()
    (project / ".git").mkdir()
    store = Project.open(project).state
    store.create_project("test", now=datetime(2026, 1, 1, tzinfo=UTC))
    store.model_cache_directory("test").mkdir(parents=True)
    (project / "agent.toml").write_text("private\n")
    (project / ".env.local").write_text("TOKEN=private\n")

    policy = build_project_path_policy(
        project,
        evaluator_source=project / "_evaluator" / "nested",
    )
    state_paths = store.sandbox_paths()

    assert set(policy.read_only_paths) == {Path(".git"), state_paths.read_only_path}
    assert state_paths.hidden_paths == ()
    assert set(policy.hidden_paths) == {Path(".env.local"), Path("agent.toml")}


def test_project_policy_ignores_external_evaluator_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "evaluator"
    external.mkdir()

    policy = build_project_path_policy(
        project,
        evaluator_source=external,
    )

    assert policy.read_only_paths == ()
    assert policy.hidden_paths == ()


def test_repository_task_policy_does_not_protect_legacy_root_names(tmp_path: Path) -> None:
    project = tmp_path / "project"
    task = project / ".vibesys" / "tasks" / "latency"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Optimize latency.\n")
    (task / "vibesys.input.toml").write_text("version = 1\n")
    for name in ("benchmark", "reference", "accuracy_checker"):
        (project / name).mkdir()

    policy = build_project_path_policy(project, evaluator_source=None)

    assert Path(".vibesys") in policy.read_only_paths
    assert not any(
        Path(name) in policy.read_only_paths
        for name in ("benchmark", "reference", "accuracy_checker")
    )
