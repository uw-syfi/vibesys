"""``harnesses.list``'s maker and generation: grouped without parsing, sent only to a client that asks.

A model's ``display`` names its maker and generation (an id, a name, and an order with 1 the
newest), so a client groups maker, generation and models without reading a name or an id. A list
that groups two ways is a contradiction the reply refuses, and a client that did not ask
(``with_generations``) never sees either field.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.host_helper_support import fake_host

from entrypoints.host_helper import vetted_harnesses
from server.host_api import (
    HarnessesListReply,
    HarnessesListRequest,
    HarnessOption,
    HelperFeature,
    request_features,
)
from server.host_harness import (
    HarnessDisplay,
    Maker,
    ModelGeneration,
    generation_conflict,
)
from server.host_helper_requests import answer
from vs_sim.api.testing import SimNetwork, SimThreads

_GENERATIONS = st.builds(
    ModelGeneration,
    id=st.sampled_from(["g1", "g2", "g3"]),
    name=st.sampled_from(["One", "Two", "Three"]),
    order=st.integers(1, 3),
)


@st.composite
def _displays(draw: st.DrawFn) -> HarnessDisplay:
    maker = draw(st.none() | st.sampled_from(Maker))
    generation = draw(st.none() | _GENERATIONS) if maker is not None else None
    return HarnessDisplay(provider_name="Provider", maker=maker, generation=generation)


_DISPLAYS = _displays()
_OPTIONS = st.lists(
    st.builds(
        HarnessOption,
        provider=st.just("codex"),
        model=st.text("abc", min_size=1, max_size=4),
        default=st.booleans(),
        display=st.none() | _DISPLAYS,
    ),
    max_size=6,
)


def _grouping(options: list[HarnessOption]) -> dict[tuple[Maker, str], set[tuple[str, int]]]:
    groups: dict[tuple[Maker, str], set[tuple[str, int]]] = {}
    for option in options:
        display = option.display
        if display is not None and display.maker is not None and display.generation is not None:
            generation = display.generation
            groups.setdefault((display.maker, generation.id), set()).add(
                (generation.name, generation.order)
            )
    return groups


@given(_OPTIONS)
def test_a_list_groups_without_contradiction_exactly_when_each_id_has_one_name_and_order(
    options: list[HarnessOption],
) -> None:
    groups = _grouping(options)
    orders: dict[tuple[Maker, int], set[str]] = {}
    for (maker, generation_id), described in groups.items():
        for _name, order in described:
            orders.setdefault((maker, order), set()).add(generation_id)
    consistent = all(len(described) == 1 for described in groups.values()) and all(
        len(ids) == 1 for ids in orders.values()
    )

    assert (generation_conflict(options) is None) == consistent
    if consistent:
        reply = HarnessesListReply(request_id="l", harnesses=tuple(options))
        assert reply.harnesses == tuple(options)
    else:
        with pytest.raises(ValidationError, match="generation"):
            HarnessesListReply(request_id="l", harnesses=tuple(options))


@given(maker=st.none() | st.sampled_from(Maker), generation=_GENERATIONS)
def test_a_generation_is_ranked_among_a_makers_so_it_needs_one(
    maker: Maker | None, generation: ModelGeneration
) -> None:
    if maker is None:
        with pytest.raises(ValidationError, match="maker"):
            HarnessDisplay(provider_name="P", generation=generation)
    else:
        assert HarnessDisplay(provider_name="P", maker=maker, generation=generation).generation


def test_generation_order_starts_at_the_newest_and_ids_are_not_empty() -> None:
    with pytest.raises(ValidationError):
        ModelGeneration(id="g", name="G", order=0)
    with pytest.raises(ValidationError):
        ModelGeneration(id="", name="G", order=1)


@given(_DISPLAYS)
def test_a_display_without_a_maker_or_generation_never_names_them_on_the_wire(
    display: HarnessDisplay,
) -> None:
    plain = HarnessOption(
        provider="c", model="m", default=True, display=display
    ).without_generations()

    assert plain.display is not None
    assert plain.display.maker is None
    assert plain.display.generation is None
    assert "maker" not in plain.model_dump_json()
    assert "generation" not in plain.model_dump_json()
    assert plain.display.model_dump(exclude={"maker", "generation"}) == display.model_dump(
        exclude={"maker", "generation"}
    )


@given(options=_OPTIONS, asked=st.sampled_from([None, False, True]))
def test_the_list_carries_makers_and_generations_exactly_for_a_client_that_asks(
    options: list[HarnessOption], *, asked: bool | None
) -> None:
    if generation_conflict(options) is not None:
        return
    threads = SimThreads()
    host = replace(fake_host(threads, SimNetwork(threads)), harnesses=lambda: tuple(options))

    reply = answer(
        HarnessesListRequest(request_id="l", deadline_ms=1, with_generations=asked),
        host,
        threads.event(),
    )

    assert isinstance(reply, HarnessesListReply)
    assert sorted(reply.harnesses, key=repr) == sorted(
        options if asked else (o.without_generations() for o in options), key=repr
    )
    if not asked:
        assert '"maker"' not in reply.model_dump_json()
        assert '"generation"' not in reply.model_dump_json()
    # What a client that asks reads, it reads back from the wire unchanged.
    assert HarnessesListReply.model_validate_json(reply.model_dump_json()) == reply


def test_asking_for_generations_needs_the_harness_generations_feature() -> None:
    asked = HarnessesListRequest(request_id="l", deadline_ms=1, with_generations=True)

    assert request_features(asked) == (
        HelperFeature.HARNESSES,
        HelperFeature.HARNESS_GENERATIONS,
    )
    assert request_features(HarnessesListRequest(request_id="l", deadline_ms=1)) == (
        HelperFeature.HARNESSES,
    )


def test_the_vetted_list_names_a_maker_and_a_consistent_generation_for_every_model() -> None:
    options = vetted_harnesses()

    assert options
    for option in options:
        assert option.display is not None, option
        assert option.display.maker is not None, option
        assert option.display.generation is not None, option
    assert generation_conflict(options) is None
