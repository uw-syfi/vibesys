"""Public surface of the candidate lifecycle policy."""

from vibesys.candidates._baseline import (
    BaselineStage,
    BaselineState,
    InputMeasurement,
    on_measurement,
    on_readings,
    on_rejected,
    propose,
    stages,
)
from vibesys.candidates._config import CandidateConfig
from vibesys.candidates._run import (
    EligibleRound,
    RunPhase,
    Winner,
    after_adoption,
    choose_winner,
    ending_reason,
    proposal,
    selection_of,
    stop,
)
from vibesys.candidates._settlement import (
    STOPPED,
    FinishedAttempt,
    assessment,
    beats_baseline,
    measurement,
    outcome_of,
    settle_for,
)

__all__ = [
    "STOPPED",
    "BaselineStage",
    "BaselineState",
    "CandidateConfig",
    "EligibleRound",
    "FinishedAttempt",
    "InputMeasurement",
    "RunPhase",
    "Winner",
    "after_adoption",
    "assessment",
    "beats_baseline",
    "choose_winner",
    "ending_reason",
    "measurement",
    "on_measurement",
    "on_readings",
    "on_rejected",
    "outcome_of",
    "proposal",
    "propose",
    "selection_of",
    "settle_for",
    "stages",
    "stop",
]
