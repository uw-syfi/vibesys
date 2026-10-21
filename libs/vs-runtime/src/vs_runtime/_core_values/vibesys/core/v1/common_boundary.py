# Generated from vibesys/core/v1/common.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api as values
from vs_proto.api import core_common as pb
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def encode_RunStatus(v: values.RunStatus | None) -> pb.RunStatus:
    return {
        "running": pb.RunStatus.RUNNING,
        "paused": pb.RunStatus.PAUSED,
        "closing": pb.RunStatus.CLOSING,
        "terminal": pb.RunStatus.TERMINAL,
        "blocked": pb.RunStatus.BLOCKED,
    }.get(v, pb.RunStatus.UNSPECIFIED)


def decode_RunStatus(v: pb.RunStatus) -> values.RunStatus:
    return values.RunStatus(
        {1: "running", 2: "paused", 3: "closing", 4: "terminal", 5: "blocked"}[v]
    )


def encode_PauseOwner(v: values.PauseOwner | None) -> pb.PauseOwner:
    return {"operator": pb.PauseOwner.OPERATOR, "quota": pb.PauseOwner.QUOTA}.get(
        v, pb.PauseOwner.UNSPECIFIED
    )


def decode_PauseOwner(v: pb.PauseOwner) -> values.PauseOwner:
    return values.PauseOwner({1: "operator", 2: "quota"}[v])


def encode_StopMode(v: values.StopMode | None) -> pb.StopMode:
    return {"drain": pb.StopMode.DRAIN, "cancel": pb.StopMode.CANCEL}.get(
        v, pb.StopMode.UNSPECIFIED
    )


def decode_StopMode(v: pb.StopMode) -> values.StopMode:
    return values.StopMode({1: "drain", 2: "cancel"}[v])


def encode_ResumeWhen(v: values.ResumeWhen | None) -> pb.ResumeWhen:
    return {"now": pb.ResumeWhen.NOW, "capacity_returns": pb.ResumeWhen.CAPACITY_RETURNS}.get(
        v, pb.ResumeWhen.UNSPECIFIED
    )


def decode_ResumeWhen(v: pb.ResumeWhen) -> values.ResumeWhen:
    return cast(values.ResumeWhen, {1: "now", 2: "capacity_returns"}[v])


def encode_InputMode(v: values.InputMode | None) -> pb.InputMode:
    return {
        "next-turn": pb.InputMode.NEXT_TURN,
        "interrupt": pb.InputMode.INTERRUPT,
        "standing": pb.InputMode.STANDING,
    }.get(v, pb.InputMode.UNSPECIFIED)


def decode_InputMode(v: pb.InputMode) -> values.InputMode:
    return values.InputMode({1: "next-turn", 2: "interrupt", 3: "standing"}[v])


def encode_ControlAction(v: values.ControlAction | None) -> pb.ControlAction:
    return {
        "pause": pb.ControlAction.PAUSE,
        "resume": pb.ControlAction.RESUME,
        "stop": pb.ControlAction.STOP,
        "steer": pb.ControlAction.STEER,
        "withdraw": pb.ControlAction.WITHDRAW,
        "cancel_attempt": pb.ControlAction.CANCEL_ATTEMPT,
        "cancel_item": pb.ControlAction.CANCEL_ITEM,
        "set_max_parallel": pb.ControlAction.SET_MAX_PARALLEL,
    }.get(v, pb.ControlAction.UNSPECIFIED)


def decode_ControlAction(v: pb.ControlAction) -> values.ControlAction:
    return cast(
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
        }[v],
    )


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
}
