# Generated from vibesys/core/v1/control.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api as values
from vs_proto.api import core_control as pb
from vs_runtime._core_values.vibesys.core.v1.common_boundary import (
    decode_ArtifactRef,
    decode_AttemptId,
    decode_ControlAction,
    decode_ControlId,
    decode_InputId,
    decode_InputMode,
    decode_InvocationRef,
    decode_ItemId,
    decode_PauseOwner,
    decode_PoolId,
    decode_ResumeWhen,
    decode_RoleId,
    decode_Scope,
    decode_StopMode,
    encode_ArtifactRef,
    encode_AttemptId,
    encode_ControlAction,
    encode_ControlId,
    encode_InputId,
    encode_InputMode,
    encode_InvocationRef,
    encode_ItemId,
    encode_PauseOwner,
    encode_PoolId,
    encode_ResumeWhen,
    encode_RoleId,
    encode_Scope,
    encode_StopMode,
)
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError("Required protobuf message is absent")
    return value


def encode_ScopeInputTarget(v: values.ScopeInputTarget) -> pb.ScopeInputTarget:
    return pb.ScopeInputTarget(kind=v.kind, scope=encode_Scope(v.scope))


def decode_ScopeInputTarget(m: pb.ScopeInputTarget) -> values.ScopeInputTarget:
    return values.ScopeInputTarget(
        kind=cast(Literal["scope"], m.kind), scope=decode_Scope(_required(m.scope))
    )


def encode_ItemInputTarget(v: values.ItemInputTarget) -> pb.ItemInputTarget:
    return pb.ItemInputTarget(kind=v.kind, item_id=encode_ItemId(v.item_id))


def decode_ItemInputTarget(m: pb.ItemInputTarget) -> values.ItemInputTarget:
    return values.ItemInputTarget(
        kind=cast(Literal["item"], m.kind), item_id=decode_ItemId(_required(m.item_id))
    )


def encode_InvocationInputTarget(v: values.InvocationInputTarget) -> pb.InvocationInputTarget:
    return pb.InvocationInputTarget(kind=v.kind, invocation=encode_InvocationRef(v.invocation))


def decode_InvocationInputTarget(m: pb.InvocationInputTarget) -> values.InvocationInputTarget:
    return values.InvocationInputTarget(
        kind=cast(Literal["invocation"], m.kind),
        invocation=decode_InvocationRef(_required(m.invocation)),
    )


def encode_RoleInputTarget(v: values.RoleInputTarget) -> pb.RoleInputTarget:
    return pb.RoleInputTarget(kind=v.kind, role_id=encode_RoleId(v.role_id))


def decode_RoleInputTarget(m: pb.RoleInputTarget) -> values.RoleInputTarget:
    return values.RoleInputTarget(
        kind=cast(Literal["role"], m.kind), role_id=decode_RoleId(_required(m.role_id))
    )


def encode_InputTarget(
    v: values.ScopeInputTarget
    | values.ItemInputTarget
    | values.InvocationInputTarget
    | values.RoleInputTarget,
) -> pb.InputTarget:
    if type(v) is values.ScopeInputTarget:
        return pb.InputTarget(target=Oneof("scope", encode_ScopeInputTarget(v)))
    if type(v) is values.ItemInputTarget:
        return pb.InputTarget(target=Oneof("item", encode_ItemInputTarget(v)))
    if type(v) is values.InvocationInputTarget:
        return pb.InputTarget(target=Oneof("invocation", encode_InvocationInputTarget(v)))
    if type(v) is values.RoleInputTarget:
        return pb.InputTarget(target=Oneof("role", encode_RoleInputTarget(v)))
    raise TypeError("Invalid InputTarget member: " + type(v).__name__)


def decode_InputTarget(
    m: pb.InputTarget,
) -> (
    values.ScopeInputTarget
    | values.ItemInputTarget
    | values.InvocationInputTarget
    | values.RoleInputTarget
):
    match m.target:
        case Oneof("scope", value):
            return decode_ScopeInputTarget(value)
        case Oneof("item", value):
            return decode_ItemInputTarget(value)
        case Oneof("invocation", value):
            return decode_InvocationInputTarget(value)
        case Oneof("role", value):
            return decode_RoleInputTarget(value)
        case _:
            raise ValueError("Missing InputTarget member")


def encode_PoolCapacity(v: values.PoolCapacity) -> pb.PoolCapacity:
    return pb.PoolCapacity(pool_id=encode_PoolId(v.pool_id), capacity=str(v.capacity))


def decode_PoolCapacity(m: pb.PoolCapacity) -> values.PoolCapacity:
    return values.PoolCapacity(
        pool_id=decode_PoolId(_required(m.pool_id)), capacity=int(m.capacity)
    )


def encode_RequestRetries(v: values.RequestRetries) -> pb.RequestRetries:
    return pb.RequestRetries(limit=v.limit, backoff=v.backoff, backoff_cap=v.backoff_cap)


def decode_RequestRetries(m: pb.RequestRetries) -> values.RequestRetries:
    return values.RequestRetries(limit=m.limit, backoff=m.backoff, backoff_cap=m.backoff_cap)


def encode_Limits(v: values.Limits) -> pb.Limits:
    return pb.Limits(
        max_attempts=str(v.max_attempts),
        max_turns=str(v.max_turns),
        max_parallel=str(v.max_parallel),
        pool_capacities=[encode_PoolCapacity(x) for x in v.pool_capacities],
        max_refunds=str(v.max_refunds),
        max_adoption_rounds=str(v.max_adoption_rounds),
        request_retries=encode_RequestRetries(v.request_retries),
        cancellation_bound=v.cancellation_bound,
        cleanup_bound=v.cleanup_bound,
        reconciliation_bound=v.reconciliation_bound,
        observe_interval=v.observe_interval,
        observe_backoff_cap=v.observe_backoff_cap,
        queue_wait_budget=v.queue_wait_budget,
        queue_notice_interval=v.queue_notice_interval,
    )


def decode_Limits(m: pb.Limits) -> values.Limits:
    return values.Limits(
        max_attempts=int(m.max_attempts),
        max_turns=int(m.max_turns),
        max_parallel=int(m.max_parallel),
        pool_capacities=tuple(decode_PoolCapacity(x) for x in m.pool_capacities),
        max_refunds=int(m.max_refunds),
        max_adoption_rounds=int(m.max_adoption_rounds),
        request_retries=decode_RequestRetries(_required(m.request_retries)),
        cancellation_bound=m.cancellation_bound,
        cleanup_bound=m.cleanup_bound,
        reconciliation_bound=m.reconciliation_bound,
        observe_interval=m.observe_interval,
        observe_backoff_cap=m.observe_backoff_cap,
        queue_wait_budget=m.queue_wait_budget,
        queue_notice_interval=m.queue_notice_interval,
    )


def encode_RunPause(v: values.RunPause) -> pb.RunPause:
    return pb.RunPause(owner=encode_PauseOwner(v.owner), capacity_wait=v.capacity_wait)


def decode_RunPause(m: pb.RunPause) -> values.RunPause:
    return values.RunPause(owner=decode_PauseOwner(m.owner), capacity_wait=m.capacity_wait)


def encode_ControlInput(v: values.ControlInput) -> pb.ControlInput:
    return pb.ControlInput(
        control_id=encode_ControlId(v.control_id),
        action=encode_ControlAction(v.action),
        mode=encode_StopMode(v.mode),
        artifact=None if v.artifact is None else encode_ArtifactRef(v.artifact),
        target=None if v.target is None else encode_InputTarget(v.target),
        input_mode=encode_InputMode(v.input_mode),
        withdraws=None if v.withdraws is None else encode_InputId(v.withdraws),
        owner=encode_PauseOwner(v.owner),
        when=encode_ResumeWhen(v.when),
        fallback=v.fallback,
        capacity_wait_stands=v.capacity_wait_stands,
        attempt=None if v.attempt is None else encode_AttemptId(v.attempt),
        item=None if v.item is None else encode_ItemId(v.item),
        max_parallel=None if v.max_parallel is None else str(v.max_parallel),
    )


def decode_ControlInput(m: pb.ControlInput) -> values.ControlInput:
    return values.ControlInput(
        control_id=decode_ControlId(_required(m.control_id)),
        action=decode_ControlAction(m.action),
        mode=None if m.mode == 0 else decode_StopMode(m.mode),
        artifact=None if m.artifact is None else decode_ArtifactRef(m.artifact),
        target=None if m.target is None else decode_InputTarget(m.target),
        input_mode=None if m.input_mode == 0 else decode_InputMode(m.input_mode),
        withdraws=None if m.withdraws is None else decode_InputId(m.withdraws),
        owner=None if m.owner == 0 else decode_PauseOwner(m.owner),
        when=None if m.when == 0 else decode_ResumeWhen(m.when),
        fallback=m.fallback,
        capacity_wait_stands=m.capacity_wait_stands,
        attempt=None if m.attempt is None else decode_AttemptId(m.attempt),
        item=None if m.item is None else decode_ItemId(m.item),
        max_parallel=int(m.max_parallel) if m.has_field("max_parallel") else None,
    )


def encode_RunPark(v: values.RunPark) -> pb.RunPark:
    return pb.RunPark(
        admitted_turns=[encode_InvocationRef(x) for x in v.admitted_turns],
        immediate=v.immediate,
        control_id=encode_ControlId(v.control_id),
        epoch=str(v.epoch),
    )


def decode_RunPark(m: pb.RunPark) -> values.RunPark:
    return values.RunPark(
        admitted_turns=tuple(decode_InvocationRef(x) for x in m.admitted_turns),
        immediate=m.immediate,
        control_id=decode_ControlId(_required(m.control_id)),
        epoch=int(m.epoch),
    )


ENCODERS: dict[type, Callable[..., Message]] = {
    values.ScopeInputTarget: encode_ScopeInputTarget,
    values.ItemInputTarget: encode_ItemInputTarget,
    values.InvocationInputTarget: encode_InvocationInputTarget,
    values.RoleInputTarget: encode_RoleInputTarget,
    values.PoolCapacity: encode_PoolCapacity,
    values.RequestRetries: encode_RequestRetries,
    values.Limits: encode_Limits,
    values.RunPause: encode_RunPause,
    values.ControlInput: encode_ControlInput,
    values.RunPark: encode_RunPark,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.ScopeInputTarget: decode_ScopeInputTarget,
    pb.ItemInputTarget: decode_ItemInputTarget,
    pb.InvocationInputTarget: decode_InvocationInputTarget,
    pb.RoleInputTarget: decode_RoleInputTarget,
    pb.InputTarget: decode_InputTarget,
    pb.PoolCapacity: decode_PoolCapacity,
    pb.RequestRetries: decode_RequestRetries,
    pb.Limits: decode_Limits,
    pb.RunPause: decode_RunPause,
    pb.ControlInput: decode_ControlInput,
    pb.RunPark: decode_RunPark,
}
