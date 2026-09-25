"""Version 4 evolve settings and resume compatibility."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibesys.errors import ConfigurationError
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.loops.evolve.orchestration import (
    EvolveOptions,
    compare_resume,
    descriptor_from_options,
    options_from_descriptor,
)
from vs_project.api import OrchestrationDescriptor


def _options() -> EvolveOptions:
    return EvolveOptions(
        max_generations=3,
        children_per_generation=2,
        k_top_inspirations=2,
        k_random_inspirations=1,
        selection_temperature=0.5,
        seed=17,
        search_policy="openevolve",
        openevolve_population_size=100,
        openevolve_archive_size=20,
        openevolve_num_islands=5,
        openevolve_migration_interval=50,
        openevolve_migration_rate=0.1,
        frontier_bias=0.7,
        bootstrap_max_attempts=5,
        keep_deployments=False,
        max_parallelism=2,
        metric_space=MetricSpace(
            objectives=(
                Objective(name="throughput", direction="max"),
                Objective(name="memory", direction="min"),
            ),
            relative_noise=0.03,
        ),
    )


def test_evolve_descriptor_round_trips_resolved_settings() -> None:
    descriptor = descriptor_from_options(_options())

    assert descriptor.id == "evolve"
    assert descriptor.config_version == 1
    assert options_from_descriptor(descriptor).max_generations == 3
    assert options_from_descriptor(descriptor).metric_space.relative_noise == 0.03
    assert "run_environment" not in descriptor.options


def test_evolve_resume_allows_only_budget_increase() -> None:
    old = descriptor_from_options(_options())
    new = descriptor_from_options(_options().model_copy(update={"max_generations": 5}))

    assert compare_resume(old, old).descriptor is None
    assert compare_resume(old, new).descriptor == new


@pytest.mark.parametrize("change", [{"seed": 18}, {"openevolve_population_size": 200}])
def test_evolve_resume_rejects_changed_search_settings(change: dict[str, object]) -> None:
    old_options = _options()
    old = descriptor_from_options(old_options)
    new = descriptor_from_options(old_options.model_copy(update=change))

    with pytest.raises(ConfigurationError, match="resuming a run cannot change"):
        compare_resume(old, new)


def test_evolve_resume_rejects_budget_decrease() -> None:
    old = descriptor_from_options(_options())
    new = descriptor_from_options(_options().model_copy(update={"max_generations": 2}))

    with pytest.raises(ConfigurationError, match="cannot decrease"):
        compare_resume(old, new)


def test_evolve_options_reject_unknown_and_invalid_values() -> None:
    descriptor = descriptor_from_options(_options())
    unknown = descriptor.model_copy(update={"options": {**descriptor.options, "unexpected": True}})
    invalid = descriptor.model_copy(
        update={"options": {**descriptor.options, "selection_temperature": 0}}
    )

    with pytest.raises(ValidationError, match="unexpected"):
        options_from_descriptor(unknown)
    with pytest.raises(ValidationError, match="selection_temperature"):
        options_from_descriptor(invalid)


def test_evolve_descriptor_rejects_wrong_id_or_version() -> None:
    descriptor = descriptor_from_options(_options())
    wrong = OrchestrationDescriptor(
        id="plain", config_version=descriptor.config_version, options=descriptor.options
    )

    with pytest.raises(ConfigurationError, match="unsupported evolve"):
        options_from_descriptor(wrong)
