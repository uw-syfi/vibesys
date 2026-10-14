"""The template variables the render owner derives from one prompt context.

A prompt test renders a template the way production does: the context's fields plus the
``reply`` variables the run offers its turn, derived from the search's roles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.reply_variables import context_variables, profiler_reply

from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vibesys.orchestration.multi.search.api import multi
from vibesys.orchestration.single.search.api import single

if TYPE_CHECKING:
    from pydantic import BaseModel

__all__ = ["context_variables", "profiler_reply", "registered_reply_models"]


def registered_reply_models() -> list[type[BaseModel]]:
    """Every reply model a strategy registers, under each capability variant a run can have."""
    return [
        *dynamic.reply_schemas.values(),
        *evolve.reply_schemas.values(),
        *issue_queue.reply_schemas.values(),
        *multi.reply_schemas.values(),
        *single.reply_schemas.values(),
    ]
