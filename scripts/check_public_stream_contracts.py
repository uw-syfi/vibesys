"""Require explicit delivery semantics for every public app-facing observation stream.

The wire schemas own each stream's fields. This check owns only the cross-contract taxonomy: whether
frames are ordered, replacements, conflated state, or a declared mixture, and what that choice means
for initial state, resumption, bounds, liveness, failure, and compatibility. It is stdlib-only so it
can run before generated bindings or project dependencies exist.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "scripts/public_stream_contracts.toml"
TOPIC_SOURCE = "proto/vibesys/api/v1/subscribe.proto"


@dataclass(frozen=True)
class Surface:
    """One app-facing service whose server streams must be classified."""

    source: str
    service: str
    compatibility: str


@dataclass(frozen=True)
class StreamMethod:
    """A discovered stream and the response message that carries its frames."""

    owner: str
    source: str
    response: str
    compatibility: str


PUBLIC_SURFACES = (
    Surface("proto/vibesys/api/v1/host.proto", "HostService", "schema_hash"),
    Surface("proto/vibesys/orchestrator/v1/service.proto", "OrchestratorService", "version_range"),
    Surface(
        "proto/vibesys/orchestrator/v1/host_control.proto",
        "HostControlService",
        "version_range",
    ),
)
SUBSCRIBE_RPC = "vibesys.api.v1.HostService/Subscribe"

SEMANTICS = {
    "delivery": frozenset({"ordered", "replacement", "conflated", "composite"}),
    "initial": frozenset(
        {"cursor", "empty_then_snapshot", "snapshot_or_resume", "page_or_resume", "current_view"}
    ),
    "ordering": frozenset(
        {
            "log_sequence",
            "byte_offset",
            "key_order",
            "per_channel",
            "committed_sequence",
            "stream_order",
        }
    ),
    "resume": frozenset({"offset", "sequence", "restart"}),
    "cursor_scope": frozenset({"durable_log", "per_channel", "none"}),
    "completeness": frozenset(
        {"lossless", "latest_per_key", "per_channel", "committed_and_ephemeral", "whole_view"}
    ),
    "bounds": frozenset({"message", "chunk", "key_value", "explicit_omission", "paged_elision"}),
    "liveness": frozenset({"transport", "heartbeat_source_lease"}),
    "failure": frozenset({"grpc_status", "terminal_frame_or_grpc"}),
    "compatibility": frozenset({"schema_hash", "version_range"}),
}
CHANNEL_SEMANTICS = frozenset(
    {"bounded_snapshot", "ordered_change", "stream_revision_view", "replacement_view"}
)
DELIVERY_REQUIREMENTS = {
    "ordered": {
        "ordering": frozenset({"log_sequence", "byte_offset", "committed_sequence"}),
        "resume": frozenset({"offset", "sequence"}),
        "cursor_scope": frozenset({"durable_log"}),
        "completeness": frozenset({"lossless", "committed_and_ephemeral"}),
    },
    "replacement": {
        "initial": frozenset({"current_view"}),
        "ordering": frozenset({"stream_order"}),
        "resume": frozenset({"restart"}),
        "cursor_scope": frozenset({"none"}),
        "completeness": frozenset({"whole_view"}),
    },
    "conflated": {
        "initial": frozenset({"empty_then_snapshot"}),
        "ordering": frozenset({"key_order"}),
        "resume": frozenset({"restart"}),
        "cursor_scope": frozenset({"none"}),
        "completeness": frozenset({"latest_per_key"}),
        "liveness": frozenset({"heartbeat_source_lease"}),
        "failure": frozenset({"terminal_frame_or_grpc"}),
    },
    "composite": {
        "initial": frozenset({"snapshot_or_resume"}),
        "ordering": frozenset({"per_channel"}),
        "resume": frozenset({"sequence"}),
        "cursor_scope": frozenset({"per_channel"}),
        "completeness": frozenset({"per_channel"}),
    },
}
REQUIRED = frozenset({"id", "owner", *SEMANTICS})
OPTIONAL = frozenset({"channel_oneof", "channels"})

_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_SERVER_STREAM = re.compile(
    r"\brpc\s+(\w+)\s*\(\s*(?:stream\s+)?[\w.]+\s*\)\s*"
    r"returns\s*\(\s*stream\s+([\w.]+)\s*\)\s*(?:;|\{)",
    re.DOTALL,
)
_FIELD = re.compile(r"(?:^|;)\s*(?:optional\s+|repeated\s+)?[\w.]+\s+(\w+)\s*=\s*\d+", re.MULTILINE)


def _without_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.DOTALL)


def _named_block(text: str, kind: str, name: str) -> str | None:
    """Return a protobuf declaration body using balanced braces."""
    clean = _without_comments(text)
    match = re.search(rf"\b{re.escape(kind)}\s+{re.escape(name)}\s*\{{", clean)
    if match is None:
        return None
    opening = clean.find("{", match.start())
    depth = 0
    for index in range(opening, len(clean)):
        if clean[index] == "{":
            depth += 1
        elif clean[index] == "}":
            depth -= 1
            if depth == 0:
                return clean[opening + 1 : index]
    return None


def _oneof_fields(text: str, message: str, oneof: str) -> tuple[str, ...] | None:
    body = _named_block(text, "message", message.rsplit(".", 1)[-1])
    choice = None if body is None else _named_block(body, "oneof", oneof)
    return None if choice is None else tuple(_FIELD.findall(choice))


def discover_streams(
    sources: Mapping[str, str],
    surfaces: Sequence[Surface] = PUBLIC_SURFACES,
    topic_source: str = TOPIC_SOURCE,
) -> tuple[dict[str, StreamMethod], list[str]]:
    """Discover server streams and Subscribe topics from their owning proto sources."""
    found: dict[str, StreamMethod] = {}
    problems: list[str] = []
    for surface in surfaces:
        text = sources.get(surface.source)
        if text is None:
            problems.append(f"{surface.source}: public-streams: source is missing")
            continue
        package = _PACKAGE.search(_without_comments(text))
        service = _named_block(text, "service", surface.service)
        if package is None or service is None:
            problems.append(
                f"{surface.source}: public-streams: cannot find package and service {surface.service}"
            )
            continue
        for method, response in _SERVER_STREAM.findall(service):
            rpc = f"{package.group(1)}.{surface.service}/{method}"
            if rpc != SUBSCRIBE_RPC:
                found[rpc] = StreamMethod(
                    surface.source, surface.source, response, surface.compatibility
                )
                continue
            topic_text = sources.get(topic_source)
            fields = (
                None
                if topic_text is None
                else _oneof_fields(topic_text, "SubscribeRequest", "topic")
            )
            if fields is None:
                problems.append(
                    f"{topic_source}: public-streams: cannot find SubscribeRequest.topic"
                )
                continue
            for field in fields:
                found[f"{rpc}#{field}"] = StreamMethod(
                    topic_source, surface.source, response, surface.compatibility
                )
    return found, problems


def _entry_map(document: Mapping[str, object]) -> tuple[dict[str, Mapping[str, object]], list[str]]:
    problems: list[str] = []
    if set(document) != {"stream"}:
        problems.append(
            f"{REGISTRY}: public-streams: top level must contain only [[stream]] entries"
        )
    raw = document.get("stream", [])
    if not isinstance(raw, list):
        return {}, [*problems, f"{REGISTRY}: public-streams: stream must be an array of tables"]
    entries: dict[str, Mapping[str, object]] = {}
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            problems.append(f"{REGISTRY}: public-streams: stream {index} needs a string id")
            continue
        stream_id = entry["id"]
        if stream_id in entries:
            problems.append(f"{REGISTRY}: public-streams: duplicate id {stream_id}")
        entries[stream_id] = entry
    return entries, problems


def _semantic_violations(
    stream_id: str, entry: Mapping[str, object], method: StreamMethod
) -> list[str]:
    keys = set(entry)
    problems = [
        f"{REGISTRY}: public-streams: {stream_id} is missing {field}"
        for field in sorted(REQUIRED - keys)
    ]
    problems.extend(
        f"{REGISTRY}: public-streams: {stream_id} has unknown field {field}"
        for field in sorted(keys - REQUIRED - OPTIONAL)
    )
    if entry.get("owner") != method.owner:
        problems.append(f"{REGISTRY}: public-streams: {stream_id} owner must be {method.owner}")
    for field, allowed in SEMANTICS.items():
        if entry.get(field) not in allowed:
            problems.append(
                f"{REGISTRY}: public-streams: {stream_id} {field} must be one of "
                f"{', '.join(sorted(allowed))}"
            )
    delivery = entry.get("delivery")
    if isinstance(delivery, str):
        for field, allowed in DELIVERY_REQUIREMENTS.get(delivery, {}).items():
            if entry.get(field) not in allowed:
                problems.append(
                    f"{REGISTRY}: public-streams: {stream_id} {delivery} delivery requires {field} "
                    f"to be one of {', '.join(sorted(allowed))}"
                )
    if entry.get("compatibility") != method.compatibility:
        problems.append(
            f"{REGISTRY}: public-streams: {stream_id} compatibility must be "
            f"{method.compatibility} for its public service"
        )
    return problems


def _channel_violations(
    stream_id: str,
    entry: Mapping[str, object],
    method: StreamMethod,
    sources: Mapping[str, str],
) -> list[str]:
    keys = set(entry)
    composite = entry.get("delivery") == "composite"
    if not composite:
        if {"channel_oneof", "channels"} & keys:
            return [
                f"{REGISTRY}: public-streams: {stream_id} declares channels but is not composite"
            ]
        return []

    oneof, channels = entry.get("channel_oneof"), entry.get("channels")
    source = sources.get(method.source, "")
    actual = _oneof_fields(source, method.response, oneof) if isinstance(oneof, str) else None
    if actual is None or not isinstance(channels, dict):
        return [
            f"{REGISTRY}: public-streams: {stream_id} needs a response channel_oneof and channels"
        ]
    problems: list[str] = []
    if set(channels) != set(actual):
        problems.append(
            f"{REGISTRY}: public-streams: {stream_id} channels {sorted(channels)} do not match "
            f"{oneof} arms {sorted(actual)}"
        )
    for channel, semantics in channels.items():
        if semantics not in CHANNEL_SEMANTICS:
            problems.append(
                f"{REGISTRY}: public-streams: {stream_id} channel {channel} has unknown semantics "
                f"{semantics}"
            )
    return problems


def violations(
    sources: Mapping[str, str],
    document: Mapping[str, object],
    surfaces: Sequence[Surface] = PUBLIC_SURFACES,
    topic_source: str = TOPIC_SOURCE,
) -> list[str]:
    """Return diagnostics for unclassified streams and invalid semantic declarations."""
    discovered, problems = discover_streams(sources, surfaces, topic_source)
    entries, entry_problems = _entry_map(document)
    problems.extend(entry_problems)
    for missing in sorted(set(discovered) - set(entries)):
        problems.append(f"{REGISTRY}: public-streams: {missing} is not classified")
    for stale in sorted(set(entries) - set(discovered)):
        problems.append(
            f"{REGISTRY}: public-streams: {stale} is not a public server stream or topic"
        )

    for stream_id in sorted(set(entries) & set(discovered)):
        entry, method = entries[stream_id], discovered[stream_id]
        problems.extend(_semantic_violations(stream_id, entry, method))
        problems.extend(_channel_violations(stream_id, entry, method, sources))
    return problems


def repository_violations(root: Path = ROOT) -> list[str]:
    """Check the repository registry against the public proto surfaces."""
    paths = {surface.source for surface in PUBLIC_SURFACES} | {TOPIC_SOURCE}
    sources = {path: (root / path).read_text(encoding="utf-8") for path in paths}
    try:
        document = tomllib.loads((root / REGISTRY).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        return [f"{REGISTRY}: public-streams: invalid TOML: {error}"]
    return violations(sources, document)


def main() -> int:
    """Print violations and return a process status."""
    problems = repository_violations()
    for problem in problems:
        print(problem)
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
