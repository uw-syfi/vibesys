"""Individual built-in policy registrations for application catalog assembly.

This facade publishes policy definitions without selecting a default registry.
"""

from vibesys.orchestration.dynamic import REGISTRATION as DYNAMIC
from vibesys.orchestration.evolve import REGISTRATION as EVOLVE
from vibesys.orchestration.input_check import REGISTRATION as INPUT_CHECK
from vibesys.orchestration.issue_queue import REGISTRATION as ISSUE_QUEUE
from vibesys.orchestration.multi import (
    PROFILE_GUIDED_REGISTRATION as PROFILE_GUIDED_MULTI,
)
from vibesys.orchestration.multi import REGISTRATION as MULTI
from vibesys.orchestration.single import (
    PROFILE_GUIDED_REGISTRATION as PROFILE_GUIDED_SINGLE,
)
from vibesys.orchestration.single import REGISTRATION as SINGLE

BUILT_INS = (
    SINGLE,
    INPUT_CHECK,
    DYNAMIC,
    PROFILE_GUIDED_SINGLE,
    MULTI,
    PROFILE_GUIDED_MULTI,
    ISSUE_QUEUE,
    EVOLVE,
)

__all__ = [
    "DYNAMIC",
    "EVOLVE",
    "INPUT_CHECK",
    "ISSUE_QUEUE",
    "MULTI",
    "PROFILE_GUIDED_MULTI",
    "PROFILE_GUIDED_SINGLE",
    "SINGLE",
]
