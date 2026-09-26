"""Version 4 evolve settings and resume compatibility."""

from __future__ import annotations

from typing import Literal

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
from vibesys.loops.evolve.run import _resolve_selector
from vibesys.search.population.models import OpenEvolveSelectorConfig
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


def _selector_options(
    *,
    search_policy: Literal["vibesys", "openevolve"] | None,
    openevolve_config: OpenEvolveSelectorConfig | None,
) -> EvolveOptions:
    return EvolveOptions(
        max_generations=1,
        children_per_generation=1,
        k_top_inspirations=0,
        k_random_inspirations=0,
        selection_temperature=0.5,
        search_policy=search_policy,
        openevolve_population_size=(
            openevolve_config.population_size if openevolve_config else None
        ),
        openevolve_archive_size=openevolve_config.archive_size if openevolve_config else None,
        openevolve_num_islands=openevolve_config.num_islands if openevolve_config else None,
        openevolve_migration_interval=(
            openevolve_config.migration_interval if openevolve_config else None
        ),
        openevolve_migration_rate=(openevolve_config.migration_rate if openevolve_config else None),
        frontier_bias=0.7,
        bootstrap_max_attempts=1,
        keep_deployments=False,
        max_parallelism=1,
    )


def test_programmatic_openevolve_config_infers_policy() -> None:
    """A run built from an ``OpenEvolveSelectorConfig`` (not from CLI flags)
    still resolves to the OpenEvolve selector without an explicit policy."""
    options = _selector_options(
        search_policy=None, openevolve_config=OpenEvolveSelectorConfig(num_islands=1)
    )

    selector, config = _resolve_selector(options, existing=None)

    assert selector == "openevolve"
    assert config is not None
    assert config.num_islands == 1


def test_programmatic_openevolve_config_rejects_vibesys_policy() -> None:
    # ``EvolveOptions`` itself already forbids constructing this combination
    # (its ``_validate_search_policy_settings`` validator); ``model_copy``
    # bypasses that validator, so it can still build one to exercise
    # ``_resolve_selector``'s own defensive check.
    options = _selector_options(search_policy=None, openevolve_config=None).model_copy(
        update={"search_policy": "vibesys", "openevolve_num_islands": 3},
    )

    with pytest.raises(ValueError, match="requires the OpenEvolve search policy"):
        _resolve_selector(options, existing=None)
