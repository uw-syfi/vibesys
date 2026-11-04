"""The mark a step's processes carry, and the handle of an evaluation that it names.

A local evaluation's stage commands and an agent turn's processes carry a mark that names the
run and the step's evaluation handle (``stage_mark``, ``agent_mark``), so one reaper finds both
and two runs on one host never reap each other.
"""

from vs_runtime._evaluation_jobs import handle_for, handle_of_life, is_handle_of
from vs_runtime._stage_marks import agent_mark, is_stage_mark_of, stage_mark

__all__ = [
    "agent_mark",
    "handle_for",
    "handle_of_life",
    "is_handle_of",
    "is_stage_mark_of",
    "stage_mark",
]
