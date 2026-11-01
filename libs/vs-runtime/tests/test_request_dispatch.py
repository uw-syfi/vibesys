"""Every request core can issue is routed to exactly one executor role."""

from __future__ import annotations

import typing
from types import UnionType
from typing import Annotated, get_args, get_origin

import pytest

from vs_core.api import (
    Access,
    ContractError,
    EnsureSession,
    HostFence,
    HostId,
    Request,
    RequestId,
    RoleId,
    RunId,
    Scope,
    SessionId,
    SessionSpec,
    Until,
)
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    ExecutionContext,
    ExecutorRole,
    RequestExecutors,
)


def variants(annotation: object) -> set[type]:
    """The concrete classes of a (possibly aliased, annotated or union) annotation."""
    if hasattr(annotation, "__value__"):
        return variants(annotation.__value__)
    origin = get_origin(annotation)
    if origin is Annotated:
        return variants(get_args(annotation)[0])
    if origin is UnionType:
        return set().union(*(variants(child) for child in get_args(annotation)))
    assert isinstance(annotation, type)
    return {annotation}


@pytest.mark.parametrize("role", list(ExecutorRole))
def test_each_role_protocol_accepts_exactly_the_requests_routed_to_it(role: ExecutorRole) -> None:
    protocol = typing.get_type_hints(RequestExecutors)[role.value]
    accepted = variants(typing.get_type_hints(protocol.execute)["request"])
    routed = {kind for kind, owner in REQUEST_DISPATCH.items() if owner == role}
    assert accepted == routed


def test_every_request_variant_is_routed_to_exactly_one_role() -> None:
    assert set(REQUEST_DISPATCH) == variants(Request)


@pytest.mark.asyncio
async def test_unrouted_request_type_is_a_typed_contract_error() -> None:
    class Unrouted(EnsureSession):
        pass

    request = Unrouted(
        request_id=RequestId(root="unrouted"),
        scope=Scope(owner=RunId(root="run"), generation=0),
        spec=SessionSpec(
            session_id=SessionId(root="session"),
            role_id=RoleId(root="implementer"),
            policy="fresh",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    context = ExecutionContext(
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        now_at=1,
        payload_digest="digest",
        until=Until(at=100.0),
    )
    with pytest.raises(ContractError, match="no executor role"):
        await RequestExecutors().dispatch(request, context)
