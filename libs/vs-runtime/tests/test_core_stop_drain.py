"""A stop waits for core's own drain requests and gives up only on work core cannot end."""

from __future__ import annotations

from typing import cast

import pytest

from vs_core.api import (
    DispatchTurn,
    DrainRole,
    EnsureSession,
    ObserveOwnedJob,
    Request,
    RequestBase,
    ResumeSessionTurn,
    SubmitMeasurement,
    drain_role,
)
from vs_runtime.api.core import REQUEST_DISPATCH, settles_through_core


def _request(kind: type[RequestBase]) -> Request:
    """A request of ``kind`` with no fields: classification looks at the kind only."""
    return cast("Request", kind.model_construct())


def _of_role(role: DrainRole) -> list[type[RequestBase]]:
    return [kind for kind in REQUEST_DISPATCH if drain_role(_request(kind)) is role]


@pytest.mark.parametrize("kind", _of_role(DrainRole.CLEANUP), ids=lambda kind: kind.__name__)
def test_a_cleanup_request_in_flight_never_makes_a_stop_give_up(kind: type[RequestBase]) -> None:
    assert settles_through_core(_request(kind), ())


@pytest.mark.parametrize(
    "kind", [SubmitMeasurement, ObserveOwnedJob, EnsureSession], ids=lambda kind: kind.__name__
)
def test_work_core_has_no_cancellation_for_is_not_waited_for(kind: type[RequestBase]) -> None:
    assert drain_role(_request(kind)) is DrainRole.WORK
    assert not settles_through_core(_request(kind), ())


@pytest.mark.parametrize("kind", [DispatchTurn, ResumeSessionTurn], ids=lambda kind: kind.__name__)
def test_a_turn_is_work_until_core_asks_to_cancel_it(kind: type[RequestBase]) -> None:
    assert drain_role(_request(kind)) is DrainRole.WORK
