"""The ``reply`` a template reads offers the fields the agent's provider actually receives.

The render owner derives ``reply`` from ``model_json_schema()``; a provider is sent that
schema after its dialect rewrote it. Prompts and agents agree only if the rewrite never
adds, drops, or renames a reachable property, for any registered reply model and provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.prompt_context import registered_reply_models

from vs_agent.api import CLI_VERSIONS, native_schema
from vs_prompts.api import reply_fields

if TYPE_CHECKING:
    from pydantic import BaseModel


@pytest.mark.parametrize("provider", sorted(CLI_VERSIONS))
@pytest.mark.parametrize("model", registered_reply_models(), ids=lambda model: model.__name__)
def test_a_provider_receives_the_fields_the_reply_offers(
    model: type[BaseModel], provider: str
) -> None:
    received = native_schema(model, provider)

    assert set(reply_fields(received)) == set(reply_fields(model.model_json_schema()))
