"""The variables the render owner gives a dynamic template for one prompt context.

A prompt test renders a template the way the production owner does: the context's fields
plus ``reply``, the fields the run offers the context's turn, derived from the reply type of
the role the context asks. A template naming a field the reply does not have fails the render
here, as it would in the run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.reply_variables import context_variables as variables_of

from vibesys.orchestration.dynamic.search.api import dynamic

if TYPE_CHECKING:
    from pydantic import BaseModel


def context_variables(context: BaseModel) -> dict[str, object]:
    """The context's fields (minus ``template``) and its ``reply``, as production renders."""
    return variables_of(context, dynamic.reply_schemas)
