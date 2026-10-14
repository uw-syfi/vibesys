"""Evolve resolves the operator's OpenEvolve overrides against its own defaults.

A knob the operator gave is kept as given (a rate of exactly 0.0 is a choice, not an absence);
a knob left out takes the evolve-owned default; with no knob at all nothing is resolved and the
search policy is left alone.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vibesys.orchestration.evolve.models import OpenEvolveOverrides, resolve_openevolve_options
from vibesys.orchestration.evolve.population.models import OpenEvolveSelectorConfig
from vs_sim.api.testing import examples

_DEFAULTS = OpenEvolveSelectorConfig()
_KNOBS = {
    "openevolve_population_size": _DEFAULTS.population_size,
    "openevolve_archive_size": _DEFAULTS.archive_size,
    "openevolve_num_islands": _DEFAULTS.num_islands,
    "openevolve_migration_interval": _DEFAULTS.migration_interval,
    "openevolve_migration_rate": _DEFAULTS.migration_rate,
}
_GIVEN = st.fixed_dictionaries(
    {},
    optional={
        "openevolve_population_size": st.integers(1, 5000),
        "openevolve_archive_size": st.integers(1, 500),
        "openevolve_num_islands": st.integers(1, 20),
        "openevolve_migration_interval": st.integers(1, 200),
        "openevolve_migration_rate": st.floats(0.0, 1.0, allow_nan=False),
    },
)


@example(given={"openevolve_migration_rate": 0.0}, policy=None)
@example(given={}, policy="custom")
@settings(max_examples=examples(pr=15, nightly=200), deadline=None, database=None)
@given(given=_GIVEN, policy=st.none() | st.just("custom"))
def test_a_given_knob_is_kept_as_given_and_a_missing_one_takes_the_default(
    given: dict[str, int | float], policy: str | None
) -> None:
    overrides: OpenEvolveOverrides = {**given}  # ty: ignore[invalid-assignment]

    chosen_policy, resolved = resolve_openevolve_options(policy, overrides)

    if not given:
        assert chosen_policy == policy
        assert resolved == dict.fromkeys(_KNOBS)
        return
    assert chosen_policy == (policy or "openevolve")
    assert resolved == {name: given.get(name, default) for name, default in _KNOBS.items()}
