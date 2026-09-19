"""Closed sets owned by Python domain code must match their proto enums.

The proto enums are the wire contract; the domain vocabularies live in the
packages that own the behavior. ``server.wire.enums`` maps one to the other by
name, so a member added on either side alone must fail here rather than at
runtime on a live run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args

import pytest

from server.chat.options import ChatModelSource
from server.run_lifecycle import RunStatus
from server.settings import TuiTheme
from server.wire import enums
from server.wire.v2 import common_pb2, events_pb2, requests_pb2, responses_pb2
from vibesys.agents.factory import AGENT_DRIVERS
from vibesys.loops.agent.model import HypothesisResolution, HypothesisStrategy
from vibesys.repository import RepositoryVisibility
from vibesys.schemas import CandidateDisposition, HypothesisOutcome, PerfDeltaReason
from vs_loop_state import JudgeVerdict

if TYPE_CHECKING:
    from enum import Enum


def _members(*domains: type[Enum]) -> set[str]:
    return {member.name for domain in domains for member in domain}


def _literal(annotation: object) -> set[str]:
    return {value.upper() for value in get_args(annotation)}


@pytest.mark.parametrize(
    ("proto", "domain"),
    [
        (common_pb2.RunStatus, _members(RunStatus)),
        (responses_pb2.HypothesisOutcome, _members(HypothesisOutcome, HypothesisResolution)),
        (responses_pb2.CandidateDisposition, _members(CandidateDisposition)),
        (responses_pb2.PerfDeltaReason, _members(PerfDeltaReason)),
        (responses_pb2.StrategyDisposition, _members(HypothesisStrategy)),
        (responses_pb2.RepositoryVisibility, _members(RepositoryVisibility)),
        (responses_pb2.TuiTheme, _members(TuiTheme)),
        (responses_pb2.ChatModelSource, _literal(ChatModelSource)),
        (responses_pb2.RoundReviewVerdict, _literal(JudgeVerdict)),
        (responses_pb2.ObjectiveDirection, _literal(Literal["max", "min"])),
        (events_pb2.JudgeVerdict, _literal(Literal["pass", "fail"])),
    ],
    ids=lambda value: value.DESCRIPTOR.name if hasattr(value, "DESCRIPTOR") else "domain",
)
def test_domain_closed_set_matches_the_proto_enum(proto, domain) -> None:  # noqa: ANN001
    assert enums.names(proto) == domain


def test_chat_drivers_are_a_subset_of_the_agent_drivers() -> None:
    proto = {name.lower() for name in enums.names(requests_pb2.ChatDriver)}

    assert proto == {"agentshim", "omnigent"}
    assert proto <= set(AGENT_DRIVERS)


@pytest.mark.parametrize("theme", list(TuiTheme))
def test_every_theme_maps_to_a_proto_number(theme: TuiTheme) -> None:
    assert enums.number(responses_pb2.TuiTheme, theme) != 0
    assert enums.text(responses_pb2.TuiTheme, enums.number(responses_pb2.TuiTheme, theme)) == (
        theme.name.lower()
    )
