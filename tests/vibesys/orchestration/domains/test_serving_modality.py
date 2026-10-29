"""The llm-serving pack owns the modality text; no other domain accepts a modality."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.constants import DomainName
from vibesys.core_wiring import domain_prompt_sections
from vibesys.domains.base import DOMAIN_ROLES
from vibesys.domains.registry import resolve_domain
from vibesys.errors import ConfigurationError
from vs_runtime.api import RunFacts
from vs_sim.api.testing import examples

_SERVING = resolve_domain(DomainName("llm-serving"))
_MODALITIES = st.sampled_from(_SERVING.modalities)


def _facts(domain_id: str) -> RunFacts:
    return RunFacts(
        agent_git_writable=True, domain_id=domain_id, objective="OBJ", benchmark_command="bench"
    )


def test_serving_pack_declares_text_generation_as_default() -> None:
    assert _SERVING.default_modality == "text_generation"
    assert "kv_store" not in _SERVING.modalities


@settings(max_examples=examples(pr=8, nightly=100))
@given(modality=_MODALITIES)
def test_every_serving_modality_changes_the_implementer_section(modality: str) -> None:
    facts = _facts("llm-serving")
    chosen = domain_prompt_sections(facts, modality=modality)
    others = {
        domain_prompt_sections(facts, modality=other)["implementer"]
        for other in _SERVING.modalities
        if other != modality
    }
    assert chosen["implementer"] not in others


def test_omitted_modality_renders_the_default() -> None:
    facts = _facts("llm-serving")
    assert domain_prompt_sections(facts) == domain_prompt_sections(
        facts, modality="text_generation"
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(modality=_MODALITIES)
def test_modality_text_never_reaches_a_generic_run(modality: str) -> None:
    sections = domain_prompt_sections(_facts("generic"))
    assert set(sections) == {role.value for role in DOMAIN_ROLES}
    with pytest.raises(ConfigurationError, match=modality):
        domain_prompt_sections(_facts("generic"), modality=modality)


def test_unknown_serving_modality_names_the_choices() -> None:
    with pytest.raises(ConfigurationError, match="kv_store"):
        domain_prompt_sections(_facts("llm-serving"), modality="kv_store")
