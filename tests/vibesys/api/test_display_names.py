"""Every built-in role has a name a person reads, from its declaration; the code ids are listed."""

from __future__ import annotations

import re

from vibesys.api.display_catalog import code_ids, display_names
from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vs_agent.api import SHIPPED_PROVIDERS, harnesses

_READABLE = re.compile(r"^[A-Z][A-Za-z0-9]*(?: [a-z0-9][A-Za-z0-9]*)*$")


def test_every_role_name_reads_as_words() -> None:
    for key, name in display_names().roles.items():
        assert _READABLE.fullmatch(name), (key, name)
        assert name != key


def test_a_role_is_named_by_its_bare_name_and_by_its_id_in_each_search() -> None:
    roles = display_names().roles
    assert roles["judge"] == "Judge"
    assert roles["dynamic-judge"] == "Judge"
    assert roles["perf-evaluator"] == "Perf evaluator"
    for search in (dynamic, issue_queue):
        for role in search.roles:
            assert roles[role.name] == role.title
            assert roles[search.ids.role_id(role.name).root] == role.title


def test_the_catalog_holds_no_harness_or_model_name() -> None:
    assert set(display_names().model_dump()) == {"roles"}


def test_the_code_ids_are_every_shipped_harness_vetted_model_and_role() -> None:
    ids = code_ids()
    assert set(ids.harnesses) == set(SHIPPED_PROVIDERS)
    assert {harness.model for harness in harnesses()} <= set(ids.models)
    assert set(ids.roles) == set(display_names().roles)
