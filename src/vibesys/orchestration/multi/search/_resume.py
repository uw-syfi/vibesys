"""What the multi root checkpoints, and resumes from after a code change: config and state."""

from typing import Literal

from vibesys.orchestration.multi.search._config import MultiConfig
from vibesys.serial_search.api import SerialState
from vs_core.api.search import Value


class MultiResume(Value):
    """The multi root's snapshot: its config and what the search knows between rounds.

    ``version`` is this model's own: a change to what it holds that an older checkpoint does not
    decode as is a new version, and the root takes the union of the versions it can resume.
    """

    kind: Literal["multi-resume"] = "multi-resume"
    version: Literal[1] = 1
    config: MultiConfig
    state: SerialState
