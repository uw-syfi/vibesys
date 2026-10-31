"""Every built-in role has a name a person reads, from its declaration; the code ids are listed."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from server.api.schema import display_names_typescript, main
from vibesys.api.display_catalog import ConflictingNamesError, code_ids, display_names
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
    assert roles["judge"] == "Reviewer"
    assert roles["dynamic-judge"] == "Reviewer"
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


def test_two_searches_that_title_one_role_differently_are_refused() -> None:
    retitled = replace(
        issue_queue,
        roles=tuple(role.model_copy(update={"title": "Other"}) for role in issue_queue.roles),
    )
    with pytest.raises(ConflictingNamesError, match=r"is titled both"):
        display_names((dynamic, retitled))


_GENERATED = (
    Path(__file__).resolve().parents[3]
    / "clients/libs/contracts/src/generated/display-names.generated.ts"
)


def test_the_generated_module_is_what_the_registry_says(tmp_path: Path) -> None:
    written = tmp_path / "out" / "names.ts"
    main(["--display-names-module", str(written)])
    assert written.read_text() == display_names_typescript()
    assert written.read_text() == _GENERATED.read_text()
    assert '"perf-evaluator": "Perf evaluator"' in written.read_text()
    assert "export const CODE_IDS" in written.read_text()
