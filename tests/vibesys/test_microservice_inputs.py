from itertools import pairwise
from pathlib import Path

import pytest
from tests.support.example_registry import require_external_repos

from vibesys.inputs import InputBundle, load_project_task
from vs_project.api import Project, ProjectLayoutError

PROJECT_ROOT = Path(__file__).parents[2]
MICROSERVICE_ROOT = PROJECT_ROOT / "examples" / "microservices"
TRAIN_TICKET_ROOT = MICROSERVICE_ROOT / "repositories" / "train-ticket"
# The Train Ticket task lives in a submodule, so a plain checkout does not have
# it and these assertions skip. CI's ``validate-examples`` job fetches the
# ``.vibesys`` directories and sets this, turning a missing external repository
# into a failure rather than a silent loss of coverage.
try:
    TRAIN_TICKET_LAYOUT = Project.open(TRAIN_TICKET_ROOT)
    TRAIN_TICKET_LAYOUT.discover_tasks()
except ProjectLayoutError as error:
    if require_external_repos():
        raise
    pytest.skip(
        f"Repository examples are not initialized: {error}"
        " (set VIBESYS_REQUIRE_EXAMPLE_EXTERNAL_REPOS=1 to force)",
        allow_module_level=True,
    )
TRAIN_TICKET_TASK = TRAIN_TICKET_ROOT / ".vibesys" / "tasks" / "default"


def _train_ticket_bundle() -> InputBundle:
    return load_project_task(TRAIN_TICKET_LAYOUT, TRAIN_TICKET_LAYOUT.select_task("default"))


def _adjacent_pairs(command: tuple[str, ...]) -> set[tuple[str, str]]:
    return set(pairwise(command))


def test_train_ticket_benchmark_uses_packaged_evaluator() -> None:
    bundle = _train_ticket_bundle()

    assert bundle.evaluator_path is None
    assert bundle.evaluator_package_digest is not None
    assert bundle.benchmark_command[:5] == (
        "go",
        "-C",
        str(PROJECT_ROOT / "resources" / "evaluators" / "microservice"),
        "run",
        "./cmd/servicebench",
    )
    assert bundle.benchmark_result is None
    assert bundle.benchmark_result_protocol == 2
    assert ("--seed", "random") in _adjacent_pairs(bundle.benchmark_command)
    assert ("--fixture-seed", "random") in _adjacent_pairs(bundle.benchmark_command)


def test_train_ticket_task_is_discovered() -> None:
    assert TRAIN_TICKET_TASK.is_dir()


def test_train_ticket_accuracy_uses_packaged_evaluator() -> None:
    bundle = _train_ticket_bundle()

    assert bundle.accuracy_command[:5] == (
        "go",
        "-C",
        str(PROJECT_ROOT / "resources" / "evaluators" / "microservice"),
        "run",
        "./cmd/servicebench",
    )
    assert bundle.accuracy_command[5:7] == ("--mode", "accuracy")
