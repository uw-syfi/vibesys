"""The template variables the render owner derives from one prompt context.

A prompt test renders a template the way production does: the context's fields plus the
``reply`` variables the run offers its turn, derived from the search's roles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.vibesys.reply_variables import context_variables, profiler_reply

from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.orchestration.input_check.search import check_input
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vibesys.orchestration.multi.search.api import multi
from vibesys.orchestration.single.search.api import single

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api import SearchDef

__all__ = [
    "context_variables",
    "profiler_reply",
    "registered_reply_models",
    "registered_searches",
]


def registered_searches() -> tuple[SearchDef[Any], ...]:
    """Every search a built-in orchestration runs."""
    return (check_input, dynamic, evolve, issue_queue, multi, single)


def registered_reply_models() -> list[type[BaseModel]]:
    """Every reply model a strategy registers, under each capability variant a run can have."""
    return [model for search in registered_searches() for model in search.reply_schemas.values()]
