"""The value a local evaluation's stage commands carry as their step mark.

A run's request ids repeat in another run on the same host, so the mark names the run as well as
the evaluation handle: ``<run id>:<handle>``. A reaper of one run finds only that run's marks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._evaluation_jobs import handle_for, is_handle_of

if TYPE_CHECKING:
    from vs_core.api import RequestId


def stage_mark(run_id: str, handle: str) -> str:
    """The mark of the evaluation *handle* of run *run_id*."""
    return f"{run_id}:{handle}"


def agent_mark(run_id: str, request_id: RequestId) -> str:
    """The mark of an agent turn's processes: the stage mark of the step's own handle.

    An agent and a stage command of one step carry the same kind of mark, so one reaper that
    finds a step's marked processes finds both (``is_stage_mark_of``).
    """
    return stage_mark(run_id, handle_for(request_id))


def is_stage_mark_of(run_id: str, request_id: RequestId, mark: str) -> bool:
    """Whether *mark* is one the step *request_id* of run *run_id* ran its stages under."""
    prefix = f"{run_id}:"
    return mark.startswith(prefix) and is_handle_of(request_id, mark.removeprefix(prefix))
