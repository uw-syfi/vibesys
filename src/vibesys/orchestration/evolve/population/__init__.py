"""Deterministic evolutionary population search.

Public surface: :class:`PopulationConfig`, :class:`PopulationState`, and
:class:`PopulationSearch`. Everything here is pure (see ``vibesys.search``);
orchestration in ``vibesys.orchestration.evolve`` owns persisting
:class:`PopulationState` and supplying agent/gate results as
:class:`CandidateOutcome`.
"""

from __future__ import annotations

from vibesys.orchestration.evolve.population.models import (
    CandidateOutcome,
    Individual,
    OpenEvolveSelectorConfig,
    OpenEvolveSelectorState,
    PopulationConfig,
    PopulationState,
    Proposal,
    RandomState,
)
from vibesys.orchestration.evolve.population.search import PopulationSearch, candidate_fitness

__all__ = [
    "CandidateOutcome",
    "Individual",
    "OpenEvolveSelectorConfig",
    "OpenEvolveSelectorState",
    "PopulationConfig",
    "PopulationSearch",
    "PopulationState",
    "Proposal",
    "RandomState",
    "candidate_fitness",
]
