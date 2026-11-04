"""What the single root checkpoints, and resumes from after a code change: config and state."""

from typing import Literal

from vibesys.orchestration.single.search._config import SingleConfig
from vibesys.serial_search.api import SerialState
from vs_core.api.search import Value


class SingleResume(Value):
    """The single root's snapshot: its config and what the search knows between rounds.

    ``version`` is this model's own: a change to what it holds that an older checkpoint does not
    decode as is a new version, and the root takes the union of the versions it can resume.
    """

    kind: Literal["single-resume"] = "single-resume"
    version: Literal[1] = 1
    config: SingleConfig
    state: SerialState
