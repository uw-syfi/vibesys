"""Environment-owned writable grants and verified termination, dormant until cutover.

The namespace is backing-wide, shared by every run granted access to it. Only
new native execution domains may create a registry: legacy/incomplete domains
remain Unknown. Every writer must register before launch. Sealing forbids new
grants; only destruction of every immutable execution domain proves death.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from protobuf import Message

from vs_agent.api import OrphanReapError, reap_recorded_agents
from vs_core.api.replay import ReplayWriterRecovery
from vs_proto.api import ReplayStore as ReplayRecords
from vs_proto.api import replay as pb
from vs_proto.api import validate
from vs_runtime._receipt_store import ReceiptStore
from vs_sim.api import DISCARD, ProcessTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_project.api import StateNamespace, StoreFence
    from vs_proto.api import Timestamp
    from vs_runtime._replay_runtime import ReplayRuntime
    from vs_sandbox.api import DockerCli
    from vs_sim.api import BlockingRunner, Threads


class WriterProofError(ValueError):
    """Missing, conflicting or unconfirmed writer evidence forbids backing reuse."""


class ReplayWriterBlockedError(WriterProofError):
    """A persisted prerequisite retains all outstanding identities and missing evidence."""

    def __init__(self, result: pb.WriterReconciliation) -> None:
        self.reconciliation = deepcopy(result)
        super().__init__(
            "Recovery blocked: previous writers may still modify this workspace; " + result.detail
        )


def _checked[T: Message](message: T) -> T:
    validate(message)
    if message.to_binary() != message.to_binary(write_unknown_fields=False):
        problem = "writer evidence contains unknown fields"
        raise WriterProofError(problem)
    return message


class WriterRegistry:
    """Durable grants for one backing generation, across runs and host incarnations.

    This is execution evidence, never scheduling state. A failed/uncertain write
    grants no dispatch authority. A sealed registry cannot be reopened: the next
    execution domain gets a new generation only after the prior proof is durable.
    ``namespace`` must be the same backing-wide namespace for all its writers.
    """

    def __init__(
        self, namespace: StateNamespace, backing: pb.WriterBacking, threads: Threads
    ) -> None:
        self._namespace = namespace
        self._backing = _checked(deepcopy(backing))
        self._lock = ReceiptStore(namespace, threads)
        self._reuse_owner: tuple[str, str, int] | None = None
        # One authoritative head per backing identity, across generations and authorities.
        self._path = "writers/" + hashlib.sha256(backing.identity.encode()).hexdigest() + ".pb"

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with ExitStack() as held:
            try:
                held.enter_context(self._lock.exclusive(blocking=False))
            except BlockingIOError as error:
                problem = "Recovery blocked: previous writers may still modify this workspace; backing is held by another writer"
                raise WriterProofError(problem) from error
            yield

    @contextmanager
    def reuse(self, *, run_id: str, fence: StoreFence) -> Iterator[pb.WriterTerminationProof]:
        """Keep backing exclusion from proof validation through the awaited reuse effect."""
        owner = run_id, fence.host_id, fence.epoch
        with self._exclusive():
            if self._reuse_owner is not None and self._reuse_owner != owner:
                problem = "Recovery blocked: previous writers may still modify this workspace; backing is held by another writer"
                raise WriterProofError(problem)
            previous, self._reuse_owner = self._reuse_owner, owner
            try:
                yield self.require_proof()
            finally:
                self._reuse_owner = previous

    @property
    def backing(self) -> pb.WriterBacking:
        return deepcopy(self._backing)

    def _read(self) -> pb.WriterRegistryState | None:
        payload = self._namespace.read_bytes(self._path)
        return None if payload is None else _checked(pb.WriterRegistryState.from_binary(payload))

    def _load(self) -> pb.WriterRegistryState:
        state = self._read()
        if state is None:
            problem = "Recovery blocked: previous writers may still modify this workspace; writer manifest is absent and inherited grants cannot be enumerated"
            raise WriterProofError(problem)
        if state.manifest is None or state.manifest.backing != self._backing:
            problem = "Recovery blocked: previous writers may still modify this workspace; writer registry has another active backing generation"
            raise WriterProofError(problem)
        return state

    def _save(self, state: pb.WriterRegistryState) -> None:
        self._namespace.write_bytes(self._path, _checked(state).to_binary())

    def create(self) -> None:
        """Initialize native backing or advance its proven, immutable domain generation."""
        with self._exclusive():
            previous = self._read()
            if previous is None:
                manifest = pb.WriterManifest(backing=self._backing)
            elif previous.manifest is not None and previous.manifest.backing == self._backing:
                return
            else:
                manifest = self._successor(previous)
            self._save(pb.WriterRegistryState(manifest=manifest))

    def _successor(self, previous: pb.WriterRegistryState) -> pb.WriterManifest:
        manifest = previous.manifest
        old = None if manifest is None else manifest.backing
        if (
            manifest is None
            or old is None
            or previous.proof is None
            or self._backing.generation != old.generation + 1
            or self._backing.environment != old.environment
            or self._backing.authority != old.authority
        ):
            problem = "Recovery blocked: previous writers may still modify this workspace; the next backing generation requires its predecessor's proof and the same authority"
            raise WriterProofError(problem)
        # Keep every past grant. A new generation cannot hide other runs' writers,
        # and its environment proof must verify their immutable domains again.
        return pb.WriterManifest(backing=self._backing, writers=deepcopy(manifest.writers))

    def grant(self, writer: pb.WriterIdentity) -> None:
        """Durably register before dispatch, idempotently by immutable physical ID."""
        _checked(writer)
        with self._exclusive():
            state = self._load()
            manifest = state.manifest
            if manifest is None:
                problem = "writer registry omitted its manifest"
                raise WriterProofError(problem)
            known = next((w for w in manifest.writers if w.physical_id == writer.physical_id), None)
            if known is not None:
                if known == writer and not manifest.closed:
                    return
                problem = "writer physical identity conflicts or its execution domain is closed"
                raise WriterProofError(problem)
            if manifest.closed:
                problem = "writer execution domain is closed to further grants"
                raise WriterProofError(problem)
            manifest.writers.append(deepcopy(writer))
            self._save(state)

    def close(self) -> pb.WriterManifest:
        """Commit reconciliation intent before inspection or stopping any writer."""
        with self._exclusive():
            state = self._load()
            if state.manifest is None:
                problem = "writer registry omitted its manifest"
                raise WriterProofError(problem)
            state.manifest.closed = True
            self._save(state)
            return deepcopy(state.manifest)

    def record(self, result: pb.WriterReconciliation) -> None:
        """Retain only an environment's proof for this exact closed manifest."""
        _checked(result)
        with self._exclusive():
            state = self._load()
            if state.manifest != result.manifest:
                problem = "termination evidence does not cover the complete current writer manifest"
                raise WriterProofError(problem)
            if result.proof is not None:
                state.proof = deepcopy(result.proof)
                self._save(state)

    def require_proof(self) -> pb.WriterTerminationProof:
        """Guard release, replacement, discard and import; receipts cannot supply proof."""
        with self._exclusive():
            state = self._load()
            if state.proof is None or state.proof.manifest != state.manifest:
                problem = "Recovery blocked: previous writers may still modify this workspace"
                raise WriterProofError(problem)
            return deepcopy(state.proof)


@dataclass(frozen=True)
class ReplayWriterBarrier:
    """W2-W7 shell: durable manifest, owner reconciliation, then durable proof.

    A crash at any boundary repeats inspection of the same closed domain. Native
    release guards require the log observation as well as the registry proof.
    """

    registry: WriterRegistry
    reconciler: WriterReconciler
    blocking: BlockingRunner

    def begin(self, runtime: ReplayRuntime, *, now: float, at: Timestamp) -> pb.WriterManifest:
        try:
            manifest = self.registry.close()
        except WriterProofError as error:
            recovery = ReplayWriterRecovery.from_records(
                ReplayRecords.from_binary(runtime.archive.encode()).records
            )
            manifest = recovery.intent(self.registry.backing) or pb.WriterManifest(
                backing=self.registry.backing
            )
            runtime.writer_intent(manifest, now=now, at=at)
            result = pb.WriterReconciliation(
                manifest=manifest,
                status=pb.WriterReconciliationStatus.UNKNOWN,
                detail=str(error),
            )
            runtime.writer_observation(result, now=now, at=at)
            raise ReplayWriterBlockedError(result) from error
        runtime.writer_intent(manifest, now=now, at=at)
        return manifest

    async def finish(
        self, runtime: ReplayRuntime, manifest: pb.WriterManifest, *, now: float, at: Timestamp
    ) -> pb.WriterReconciliation:
        result = await self.blocking.run(self.reconciler.reconcile_writers, manifest)
        self.registry.record(result)
        runtime.writer_observation(result, now=now, at=at)
        if result.proof is None:
            raise ReplayWriterBlockedError(result)
        return result

    async def reconcile(
        self, runtime: ReplayRuntime, *, now: float, at: Timestamp
    ) -> pb.WriterReconciliation:
        manifest = self.begin(runtime, now=now, at=at)
        return await self.finish(runtime, manifest, now=now, at=at)


class WriterReconciler(Protocol):
    """Close immutable execution domains and verify every writer and descendant dead."""

    def reconcile_writers(self, manifest: pb.WriterManifest) -> pb.WriterReconciliation: ...


@dataclass(frozen=True)
class UnsupportedWriterReconciler:
    """No supervision or remote-job evidence is available for inherited grants."""

    environment: str

    def reconcile_writers(self, manifest: pb.WriterManifest) -> pb.WriterReconciliation:
        _checked(manifest)
        if manifest.closed and not manifest.writers:
            return pb.WriterReconciliation(
                manifest=deepcopy(manifest),
                status=pb.WriterReconciliationStatus.PROVEN_STOPPED,
                proof=pb.WriterTerminationProof(manifest=deepcopy(manifest)),
            )
        return pb.WriterReconciliation(
            manifest=deepcopy(manifest),
            status=pb.WriterReconciliationStatus.UNSUPPORTED,
            detail=f"{self.environment}: authoritative termination and descendant evidence unavailable",
        )


@dataclass(frozen=True)
class DockerWriterReconciler:
    """Verify absence on the recorded daemon after reaping exact writer containers.

    Removing an immutable container closes all further exec/spawn in that domain,
    including descendants and a nested daemon. Acknowledged removal alone, a
    stopped container that can restart, and a sealed manifest prove no death.
    This owner accepts only Docker domains; remote writers need their own owner.
    """

    docker: DockerCli
    reap: Callable[[tuple[str, ...]], None]

    def _run(self, *arguments: str) -> str:
        result = self.docker.run(["docker", *arguments], timeout_seconds=60, output=DISCARD)
        if result.returncode != 0:
            problem = f"Docker writer verification failed: {result.stderr.strip()}"
            raise WriterProofError(problem)
        return result.stdout.strip()

    def _verify_daemon(self, authority: str) -> None:
        if self._run("info", "--format", "{{.ID}}") != authority:
            problem = "recorded Docker daemon is unavailable or has changed identity"
            raise WriterProofError(problem)

    def reconcile_writers(self, manifest: pb.WriterManifest) -> pb.WriterReconciliation:
        _checked(manifest)
        backing = manifest.backing
        if backing is None or backing.environment != "docker" or not manifest.closed:
            return pb.WriterReconciliation(
                manifest=deepcopy(manifest),
                status=pb.WriterReconciliationStatus.UNKNOWN,
                detail="Docker needs a complete closed Docker execution domain",
            )
        if any(re.fullmatch(r"[a-f0-9]{64}", w.domain_id) is None for w in manifest.writers):
            return pb.WriterReconciliation(
                manifest=deepcopy(manifest),
                status=pb.WriterReconciliationStatus.UNKNOWN,
                detail="Docker writer domains require full immutable container IDs",
            )
        try:
            self._verify_daemon(backing.authority)
            domains = tuple(sorted({w.domain_id for w in manifest.writers}))
            before = set(self._run("ps", "-aq", "--no-trunc").splitlines())
            self.reap(tuple(domain for domain in domains if domain in before))
            after = set(self._run("ps", "-aq", "--no-trunc").splitlines())
            self._verify_daemon(backing.authority)
            pending = sorted(after.intersection(domains))
            if pending:
                return pb.WriterReconciliation(
                    manifest=deepcopy(manifest),
                    status=pb.WriterReconciliationStatus.PENDING,
                    detail="writer containers are not verified absent: " + ", ".join(pending),
                )
            proof = pb.WriterTerminationProof(
                manifest=deepcopy(manifest), absent_domains=list(domains)
            )
            return _checked(
                pb.WriterReconciliation(
                    manifest=deepcopy(manifest),
                    status=pb.WriterReconciliationStatus.PROVEN_STOPPED,
                    proof=proof,
                )
            )
        except (WriterProofError, OrphanReapError, OSError, ProcessTimeoutError) as error:
            return pb.WriterReconciliation(
                manifest=deepcopy(manifest),
                status=pb.WriterReconciliationStatus.UNKNOWN,
                detail=str(error),
            )


def docker_writer_reconciler(
    docker: DockerCli, log: Callable[[str], None]
) -> DockerWriterReconciler:
    """Bind verification to vs-agent's provider-independent reaper, without opening a session."""

    def reap(containers: tuple[str, ...]) -> None:
        reap_recorded_agents(containers, docker=docker, log=log)

    return DockerWriterReconciler(docker, reap)
