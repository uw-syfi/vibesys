# Generated from vibesys/core/v1/control.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api as values
from vs_proto.api import core_state as pb
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError("Required protobuf message is absent")
    return value


def encode_Identity(v: values.Identity) -> pb.Identity:
    return pb.Identity(root=v.root)


def decode_Identity(m: pb.Identity) -> values.Identity:
    return values.Identity(root=m.root)


def encode_RunId(v: values.RunId) -> pb.RunId:
    return pb.RunId(root=v.root, kind=v.kind)


def decode_RunId(m: pb.RunId) -> values.RunId:
    return values.RunId(root=m.root, kind=cast(Literal["run"], m.kind))


def encode_AttemptId(v: values.AttemptId) -> pb.AttemptId:
    return pb.AttemptId(root=v.root, kind=v.kind)


def decode_AttemptId(m: pb.AttemptId) -> values.AttemptId:
    return values.AttemptId(root=m.root, kind=cast(Literal["attempt"], m.kind))


def encode_ItemId(v: values.ItemId) -> pb.ItemId:
    return pb.ItemId(root=v.root, kind=v.kind)


def decode_ItemId(m: pb.ItemId) -> values.ItemId:
    return values.ItemId(root=m.root, kind=cast(Literal["item"], m.kind))


def encode_SessionId(v: values.SessionId) -> pb.SessionId:
    return pb.SessionId(root=v.root, kind=v.kind)


def decode_SessionId(m: pb.SessionId) -> values.SessionId:
    return values.SessionId(root=m.root, kind=cast(Literal["session"], m.kind))


def encode_InvocationId(v: values.InvocationId) -> pb.InvocationId:
    return pb.InvocationId(root=v.root, kind=v.kind)


def decode_InvocationId(m: pb.InvocationId) -> values.InvocationId:
    return values.InvocationId(root=m.root, kind=cast(Literal["invocation"], m.kind))


def encode_ContinuationId(v: values.ContinuationId) -> pb.ContinuationId:
    return pb.ContinuationId(root=v.root, kind=v.kind)


def decode_ContinuationId(m: pb.ContinuationId) -> values.ContinuationId:
    return values.ContinuationId(root=m.root, kind=cast(Literal["continuation"], m.kind))


def encode_InputId(v: values.InputId) -> pb.InputId:
    return pb.InputId(root=v.root, kind=v.kind)


def decode_InputId(m: pb.InputId) -> values.InputId:
    return values.InputId(root=m.root, kind=cast(Literal["input"], m.kind))


def encode_EvidenceId(v: values.EvidenceId) -> pb.EvidenceId:
    return pb.EvidenceId(root=v.root, kind=v.kind)


def decode_EvidenceId(m: pb.EvidenceId) -> values.EvidenceId:
    return values.EvidenceId(root=m.root, kind=cast(Literal["evidence"], m.kind))


def encode_DecisionId(v: values.DecisionId) -> pb.DecisionId:
    return pb.DecisionId(root=v.root, kind=v.kind)


def decode_DecisionId(m: pb.DecisionId) -> values.DecisionId:
    return values.DecisionId(root=m.root, kind=cast(Literal["decision"], m.kind))


def encode_RequestId(v: values.RequestId) -> pb.RequestId:
    return pb.RequestId(root=v.root, kind=v.kind)


def decode_RequestId(m: pb.RequestId) -> values.RequestId:
    return values.RequestId(root=m.root, kind=cast(Literal["request"], m.kind))


def encode_EventId(v: values.EventId) -> pb.EventId:
    return pb.EventId(root=v.root, kind=v.kind)


def decode_EventId(m: pb.EventId) -> values.EventId:
    return values.EventId(root=m.root, kind=cast(Literal["event"], m.kind))


def encode_SettlementId(v: values.SettlementId) -> pb.SettlementId:
    return pb.SettlementId(root=v.root, kind=v.kind)


def decode_SettlementId(m: pb.SettlementId) -> values.SettlementId:
    return values.SettlementId(root=m.root, kind=cast(Literal["settlement"], m.kind))


def encode_StrategyId(v: values.StrategyId) -> pb.StrategyId:
    return pb.StrategyId(root=v.root, kind=v.kind)


def decode_StrategyId(m: pb.StrategyId) -> values.StrategyId:
    return values.StrategyId(root=m.root, kind=cast(Literal["strategy"], m.kind))


def encode_OperationId(v: values.OperationId) -> pb.OperationId:
    return pb.OperationId(root=v.root, kind=v.kind)


def decode_OperationId(m: pb.OperationId) -> values.OperationId:
    return values.OperationId(root=m.root, kind=cast(Literal["operation"], m.kind))


def encode_ArtifactId(v: values.ArtifactId) -> pb.ArtifactId:
    return pb.ArtifactId(root=v.root, kind=v.kind)


def decode_ArtifactId(m: pb.ArtifactId) -> values.ArtifactId:
    return values.ArtifactId(root=m.root, kind=cast(Literal["artifact"], m.kind))


def encode_ResourceId(v: values.ResourceId) -> pb.ResourceId:
    return pb.ResourceId(root=v.root, kind=v.kind)


def decode_ResourceId(m: pb.ResourceId) -> values.ResourceId:
    return values.ResourceId(root=m.root, kind=cast(Literal["resource"], m.kind))


def encode_RevisionId(v: values.RevisionId) -> pb.RevisionId:
    return pb.RevisionId(root=v.root, kind=v.kind)


def decode_RevisionId(m: pb.RevisionId) -> values.RevisionId:
    return values.RevisionId(root=m.root, kind=cast(Literal["revision"], m.kind))


def encode_RoleId(v: values.RoleId) -> pb.RoleId:
    return pb.RoleId(root=v.root, kind=v.kind)


def decode_RoleId(m: pb.RoleId) -> values.RoleId:
    return values.RoleId(root=m.root, kind=cast(Literal["role"], m.kind))


def encode_PoolId(v: values.PoolId) -> pb.PoolId:
    return pb.PoolId(root=v.root, kind=v.kind)


def decode_PoolId(m: pb.PoolId) -> values.PoolId:
    return values.PoolId(root=m.root, kind=cast(Literal["pool"], m.kind))


def encode_ChargeId(v: values.ChargeId) -> pb.ChargeId:
    return pb.ChargeId(root=v.root, kind=v.kind)


def decode_ChargeId(m: pb.ChargeId) -> values.ChargeId:
    return values.ChargeId(root=m.root, kind=cast(Literal["charge"], m.kind))


def encode_ControlId(v: values.ControlId) -> pb.ControlId:
    return pb.ControlId(root=v.root, kind=v.kind)


def decode_ControlId(m: pb.ControlId) -> values.ControlId:
    return values.ControlId(root=m.root, kind=cast(Literal["control"], m.kind))


def encode_HostId(v: values.HostId) -> pb.HostId:
    return pb.HostId(root=v.root, kind=v.kind)


def decode_HostId(m: pb.HostId) -> values.HostId:
    return values.HostId(root=m.root, kind=cast(Literal["host"], m.kind))


def encode_SchemaRef(v: values.SchemaRef) -> pb.SchemaRef:
    return pb.SchemaRef(name=v.name, version=str(v.version))


def decode_SchemaRef(m: pb.SchemaRef) -> values.SchemaRef:
    return values.SchemaRef(name=m.name, version=int(m.version))


def encode_ArtifactRef(v: values.ArtifactRef) -> pb.ArtifactRef:
    return pb.ArtifactRef(
        artifact_id=encode_ArtifactId(v.artifact_id),
        digest=v.digest,
        schema_ref=None if v.schema_ref is None else encode_SchemaRef(v.schema_ref),
    )


def decode_ArtifactRef(m: pb.ArtifactRef) -> values.ArtifactRef:
    return values.ArtifactRef(
        artifact_id=decode_ArtifactId(_required(m.artifact_id)),
        digest=m.digest,
        schema_ref=None if m.schema_ref is None else decode_SchemaRef(m.schema_ref),
    )


def encode_ScopeOwner(v: values.RunId | values.AttemptId) -> pb.ScopeOwner:
    if type(v) is values.RunId:
        return pb.ScopeOwner(owner=Oneof("run", encode_RunId(v)))
    if type(v) is values.AttemptId:
        return pb.ScopeOwner(owner=Oneof("attempt", encode_AttemptId(v)))
    raise TypeError("Invalid ScopeOwner member: " + type(v).__name__)


def decode_ScopeOwner(m: pb.ScopeOwner) -> values.RunId | values.AttemptId:
    match m.owner:
        case Oneof("run", value):
            return decode_RunId(value)
        case Oneof("attempt", value):
            return decode_AttemptId(value)
        case _:
            raise ValueError("Missing ScopeOwner member")


def encode_Scope(v: values.Scope) -> pb.Scope:
    return pb.Scope(owner=encode_ScopeOwner(v.owner), generation=str(v.generation))


def decode_Scope(m: pb.Scope) -> values.Scope:
    return values.Scope(owner=decode_ScopeOwner(_required(m.owner)), generation=int(m.generation))


def encode_InvocationRef(v: values.InvocationRef) -> pb.InvocationRef:
    return pb.InvocationRef(
        session_id=encode_SessionId(v.session_id),
        invocation_id=encode_InvocationId(v.invocation_id),
        generation=str(v.generation),
    )


def decode_InvocationRef(m: pb.InvocationRef) -> values.InvocationRef:
    return values.InvocationRef(
        session_id=decode_SessionId(_required(m.session_id)),
        invocation_id=decode_InvocationId(_required(m.invocation_id)),
        generation=int(m.generation),
    )


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
    return pb.RunPause(
        owner={"operator": pb.PauseOwner.OPERATOR, "quota": pb.PauseOwner.QUOTA}.get(
            v.owner, pb.PauseOwner.UNSPECIFIED
        ),
        capacity_wait=v.capacity_wait,
    )


def decode_RunPause(m: pb.RunPause) -> values.RunPause:
    return values.RunPause(
        owner=values.PauseOwner({1: "operator", 2: "quota"}[m.owner]), capacity_wait=m.capacity_wait
    )


def encode_ControlInput(v: values.ControlInput) -> pb.ControlInput:
    return pb.ControlInput(
        control_id=encode_ControlId(v.control_id),
        action={
            "pause": pb.ControlAction.PAUSE,
            "resume": pb.ControlAction.RESUME,
            "stop": pb.ControlAction.STOP,
            "steer": pb.ControlAction.STEER,
            "withdraw": pb.ControlAction.WITHDRAW,
            "cancel_attempt": pb.ControlAction.CANCEL_ATTEMPT,
            "cancel_item": pb.ControlAction.CANCEL_ITEM,
            "set_max_parallel": pb.ControlAction.SET_MAX_PARALLEL,
        }.get(v.action, pb.ControlAction.UNSPECIFIED),
        mode={"drain": pb.StopMode.DRAIN, "cancel": pb.StopMode.CANCEL}.get(
            v.mode, pb.StopMode.UNSPECIFIED
        ),
        artifact=None if v.artifact is None else encode_ArtifactRef(v.artifact),
        target=None if v.target is None else encode_InputTarget(v.target),
        input_mode={
            "next-turn": pb.InputMode.NEXT_TURN,
            "interrupt": pb.InputMode.INTERRUPT,
            "standing": pb.InputMode.STANDING,
        }.get(v.input_mode, pb.InputMode.UNSPECIFIED),
        withdraws=None if v.withdraws is None else encode_InputId(v.withdraws),
        owner={"operator": pb.PauseOwner.OPERATOR, "quota": pb.PauseOwner.QUOTA}.get(
            v.owner, pb.PauseOwner.UNSPECIFIED
        ),
        when={"now": pb.ResumeWhen.NOW, "capacity_returns": pb.ResumeWhen.CAPACITY_RETURNS}.get(
            v.when, pb.ResumeWhen.UNSPECIFIED
        ),
        fallback=v.fallback,
        capacity_wait_stands=v.capacity_wait_stands,
        attempt=None if v.attempt is None else encode_AttemptId(v.attempt),
        item=None if v.item is None else encode_ItemId(v.item),
        max_parallel=None if v.max_parallel is None else str(v.max_parallel),
    )


def decode_ControlInput(m: pb.ControlInput) -> values.ControlInput:
    return values.ControlInput(
        control_id=decode_ControlId(_required(m.control_id)),
        action=cast(
            values.ControlAction,
            {
                1: "pause",
                2: "resume",
                3: "stop",
                4: "steer",
                5: "withdraw",
                6: "cancel_attempt",
                7: "cancel_item",
                8: "set_max_parallel",
            }[m.action],
        ),
        mode=None if m.mode == 0 else values.StopMode({1: "drain", 2: "cancel"}[m.mode]),
        artifact=None if m.artifact is None else decode_ArtifactRef(m.artifact),
        target=None if m.target is None else decode_InputTarget(m.target),
        input_mode=None
        if m.input_mode == 0
        else values.InputMode({1: "next-turn", 2: "interrupt", 3: "standing"}[m.input_mode]),
        withdraws=None if m.withdraws is None else decode_InputId(m.withdraws),
        owner=None if m.owner == 0 else values.PauseOwner({1: "operator", 2: "quota"}[m.owner]),
        when=None
        if m.when == 0
        else cast(values.ResumeWhen, {1: "now", 2: "capacity_returns"}[m.when]),
        fallback=m.fallback,
        capacity_wait_stands=m.capacity_wait_stands,
        attempt=None if m.attempt is None else decode_AttemptId(m.attempt),
        item=None if m.item is None else decode_ItemId(m.item),
        max_parallel=int(m.max_parallel) if m.has_field("max_parallel") else None,
    )


ENCODERS: dict[type, Callable[..., Message]] = {
    values.Identity: encode_Identity,
    values.RunId: encode_RunId,
    values.AttemptId: encode_AttemptId,
    values.ItemId: encode_ItemId,
    values.SessionId: encode_SessionId,
    values.InvocationId: encode_InvocationId,
    values.ContinuationId: encode_ContinuationId,
    values.InputId: encode_InputId,
    values.EvidenceId: encode_EvidenceId,
    values.DecisionId: encode_DecisionId,
    values.RequestId: encode_RequestId,
    values.EventId: encode_EventId,
    values.SettlementId: encode_SettlementId,
    values.StrategyId: encode_StrategyId,
    values.OperationId: encode_OperationId,
    values.ArtifactId: encode_ArtifactId,
    values.ResourceId: encode_ResourceId,
    values.RevisionId: encode_RevisionId,
    values.RoleId: encode_RoleId,
    values.PoolId: encode_PoolId,
    values.ChargeId: encode_ChargeId,
    values.ControlId: encode_ControlId,
    values.HostId: encode_HostId,
    values.SchemaRef: encode_SchemaRef,
    values.ArtifactRef: encode_ArtifactRef,
    values.Scope: encode_Scope,
    values.InvocationRef: encode_InvocationRef,
    values.ScopeInputTarget: encode_ScopeInputTarget,
    values.ItemInputTarget: encode_ItemInputTarget,
    values.InvocationInputTarget: encode_InvocationInputTarget,
    values.RoleInputTarget: encode_RoleInputTarget,
    values.PoolCapacity: encode_PoolCapacity,
    values.RequestRetries: encode_RequestRetries,
    values.Limits: encode_Limits,
    values.RunPause: encode_RunPause,
    values.ControlInput: encode_ControlInput,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.Identity: decode_Identity,
    pb.RunId: decode_RunId,
    pb.AttemptId: decode_AttemptId,
    pb.ItemId: decode_ItemId,
    pb.SessionId: decode_SessionId,
    pb.InvocationId: decode_InvocationId,
    pb.ContinuationId: decode_ContinuationId,
    pb.InputId: decode_InputId,
    pb.EvidenceId: decode_EvidenceId,
    pb.DecisionId: decode_DecisionId,
    pb.RequestId: decode_RequestId,
    pb.EventId: decode_EventId,
    pb.SettlementId: decode_SettlementId,
    pb.StrategyId: decode_StrategyId,
    pb.OperationId: decode_OperationId,
    pb.ArtifactId: decode_ArtifactId,
    pb.ResourceId: decode_ResourceId,
    pb.RevisionId: decode_RevisionId,
    pb.RoleId: decode_RoleId,
    pb.PoolId: decode_PoolId,
    pb.ChargeId: decode_ChargeId,
    pb.ControlId: decode_ControlId,
    pb.HostId: decode_HostId,
    pb.SchemaRef: decode_SchemaRef,
    pb.ArtifactRef: decode_ArtifactRef,
    pb.ScopeOwner: decode_ScopeOwner,
    pb.Scope: decode_Scope,
    pb.InvocationRef: decode_InvocationRef,
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
}
