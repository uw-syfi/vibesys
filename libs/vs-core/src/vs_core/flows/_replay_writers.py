"""Pure writer-proof guards derived exclusively from the committed native prefix."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from protobuf import Message

from vs_proto.api import replay as pb
from vs_proto.api import validate

from ._replay_sources import ReplayError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import Record


def _decode[T: Message](kind: type[T], payload: bytes) -> T:
    value = kind.from_binary(payload)
    validate(value)
    if value.to_binary() != value.to_binary(write_unknown_fields=False):
        problem = "writer evidence contains unknown fields"
        raise ReplayError(problem)
    return value


@dataclass(frozen=True)
class ReplayRootReuse:
    """A release/reuse request carries durable environment proof, never a released receipt."""

    proof_bytes: bytes

    def proof(self) -> pb.WriterTerminationProof:
        return _decode(pb.WriterTerminationProof, self.proof_bytes)


@dataclass(frozen=True)
class ReplayWriterRecovery:
    """One immutable reconciliation per backing execution domain, folded from notices."""

    results: tuple[bytes, ...] = ()

    @classmethod
    def from_records(cls, records: Iterable[Record]) -> ReplayWriterRecovery:
        intents: dict[bytes, bytes] = {}
        results: dict[bytes, bytes] = {}
        for record in records:
            if record.body is None or record.body.field != "notice":
                continue
            detail = record.body.value.detail
            if detail is None:
                continue
            name = detail.type_url.rsplit("/", 1)[-1]
            if name == pb.WriterManifest.desc().type_name:
                manifest = _decode(pb.WriterManifest, detail.value)
                validate(manifest)
                key = _intent_key(manifest)
                if key in intents and intents[key] != manifest.to_binary():
                    problem = "a closed execution domain cannot change its writer manifest"
                    raise ReplayError(problem)
                intents[key] = manifest.to_binary()
            elif name == pb.WriterReconciliation.desc().type_name:
                result = _decode(pb.WriterReconciliation, detail.value)
                validate(result)
                manifest = result.manifest
                if manifest is None or manifest.backing is None:
                    problem = "writer reconciliation omitted its manifest"
                    raise ReplayError(problem)
                key = manifest.backing.to_binary()
                if intents.get(key) != manifest.to_binary():
                    problem = "writer reconciliation requires its previously durable intent"
                    raise ReplayError(problem)
                results[key] = result.to_binary()
        return cls(tuple(results.values()))

    def require(self, backing: pb.WriterBacking) -> ReplayRootReuse:
        """Authorize root release/reuse only after the matching environment proof was committed."""
        for payload in self.results:
            result = _decode(pb.WriterReconciliation, payload)
            if result.manifest is not None and result.manifest.backing == backing:
                if result.proof is not None:
                    return ReplayRootReuse(result.proof.to_binary())
                problem = (
                    "Recovery blocked: previous writers may still modify this workspace; "
                    + result.detail
                )
                raise ReplayError(problem)
        problem = "Recovery blocked: previous writers may still modify this workspace; missing durable environment proof"
        raise ReplayError(problem)


def _intent_key(manifest: pb.WriterManifest) -> bytes:
    if manifest.backing is None:
        problem = "reconciliation intent needs its backing identity"
        raise ReplayError(problem)
    return manifest.backing.to_binary()
