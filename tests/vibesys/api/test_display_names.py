"""Every harness, vetted model and built-in role has a name a person reads, from its owner."""

from __future__ import annotations

import re

import pytest

from vibesys.api.display_catalog import DisplayNames, display_names
from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vs_agent.api import SHIPPED_PROVIDERS, harnesses, vetted_models

#: A name a person reads: starts with a capital (or is a product's own spelling), no hyphen
#: joins or underscores of an id.
_READABLE = re.compile(r"^[A-Z][A-Za-z0-9.-]*(?: [A-Za-z0-9.-]+)*$")


@pytest.fixture(scope="module")
def names() -> DisplayNames:
    return display_names()


def test_every_shipped_harness_has_a_name(names: DisplayNames) -> None:
    assert set(names.harnesses) == set(SHIPPED_PROVIDERS)
    assert names.harnesses["claude"] == "Claude Code"
    assert names.harnesses["codex"] == "Codex"


def test_every_vetted_model_has_a_name_that_is_not_its_id(names: DisplayNames) -> None:
    for harness in harnesses():
        name = names.models.get(harness.model)
        assert name, f"{harness.provider} runs {harness.model!r}, which agentshim gives no name"
        assert name != harness.model
        assert name[0].isupper()
    assert {model for provider in SHIPPED_PROVIDERS for model in vetted_models(provider)} == set(
        names.models
    )


def test_every_name_reads_as_words(names: DisplayNames) -> None:
    for vocabulary in (names.harnesses, names.models, names.roles):
        for key, name in vocabulary.items():
            assert _READABLE.fullmatch(name), (key, name)
            assert "_" not in name


def test_a_role_is_named_by_its_bare_name_and_by_its_id_in_each_search(
    names: DisplayNames,
) -> None:
    assert names.roles["judge"] == "Judge"
    assert names.roles["dynamic-judge"] == "Judge"
    assert names.roles["perf-evaluator"] == "Perf evaluator"
    for search in (dynamic, issue_queue):
        for role in search.roles:
            assert names.roles[role.name] == role.title
            assert names.roles[search.ids.role_id(role.name).root] == role.title
