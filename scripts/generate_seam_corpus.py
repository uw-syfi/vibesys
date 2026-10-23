"""Generate synthetic journals from pinned format history, never retained host data.

Run with ``uv run python scripts/generate_seam_corpus.py``. Historical source is
parsed with ast rather than imported, so generation neither executes old code
nor fills missing fields with today's model defaults. The fixtures are inputs
to the shipping saved-run importer and the conversion crash sweep.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "tests/vibesys/run/fixtures/seam-history"
EVENTS = "src/vibesys/events.py"
SPANS = "libs/vs-core/src/vs_core/flows/_spans.py"
# Parents of the commits that introduced each storage boundary.
HISTORY = {
    "pre-trace": "a2d924d9f903bac2303862546aa7f61ab86eed27",
    "pre-log": "c934bc30af21ef426f31590d1e7e7bf4ae01dcbe",
    "pre-lock": "2d4f62793f631f367f8f5e7d99ebf15dbacf958c",
    "missing-identity": "bd0e5f31f319e9a28dc02fd74bedabff1ea9f61f",
}


def source(commit: str, path: str) -> str:
    """Read public format definitions without executing their historical imports."""
    git = shutil.which("git")
    if git is None:
        message = "git is required to read format history"
        raise RuntimeError(message)
    # lint-waiver: LW-343201 [S603]; Fixed Git show argv reads pinned source without a shell. An object-store parser would duplicate Git, and a product process adapter would couple this offline generator to the product runtime.
    return subprocess.run(  # noqa: S603
        [git, "show", f"{commit}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def fields(text: str, name: str) -> list[str]:
    """The fields the historical declaration actually offered, in source order."""
    node = next(
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and node.name == name
    )
    return [
        field.target.id
        for field in node.body
        if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
    ]


def enum_value(text: str, name: str, enum: str = "CoreEventType") -> str:
    """Resolve an event spelling from that revision, not a hand-copied enum."""
    node = next(
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and node.name == enum
    )
    return next(
        ast.literal_eval(field.value)
        for field in node.body
        if isinstance(field, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in field.targets)
    )


def journal(event_source: str, span_source: str | None, *, missing_identity: bool) -> bytes:
    """Minimal historical records omit optional fields rather than inventing defaults."""
    offered = fields(event_source, "CoreEvent")
    values = {"sequence": 1, "run_id": "run-1", "timestamp": "2026-01-01T00:00:00Z"}
    first: dict[str, object] = {key: value for key, value in values.items() if key in offered}
    first["type"] = enum_value(event_source, "RUN_STARTED")
    first["data"] = {"kind": "run_started", "outer_loop": "single-agent", "input": "synthetic"}
    events = [first]
    if span_source is not None:
        span_values = {
            "id": "op:turn" if missing_identity else "flow:root",
            "parent": None,
            "type": "operation" if missing_identity else "flow",
            "name": "agent_turn" if missing_identity else "root",
            "status": "running",
            "started_at": 0.0,
        }
        span: dict[str, object] = {
            key: value for key, value in span_values.items() if key in fields(span_source, "Span")
        }
        if missing_identity:
            # SpanLabel accepted arbitrary search attrs_json, including a role with no identity.
            if "attrs_json" not in fields(span_source, "SpanLabel"):
                message = "historical label has no attrs_json"
                raise ValueError(message)
            span["label"] = {"kind": "agent_turn", "attrs_json": '{"role":"implementer"}'}
        events.append(
            {
                **first,
                "sequence": 2,
                "type": enum_value(event_source, "SPAN_STARTED"),
                "data": {"kind": "span_started", "span": span},
            }
        )
    return b"".join(json.dumps(event, sort_keys=True).encode() + b"\n" for event in events)


def main() -> None:
    """Regenerate fixtures plus their source provenance, byte for byte."""
    DESTINATION.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, commit in HISTORY.items():
        event_source = source(commit, EVENTS)
        wire_source = source(commit, "src/server/events.py")
        host_source = source(commit, "src/server/host_api.py")
        names = [
            ast.literal_eval(node.value)
            for node in ast.parse(host_source).body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "RUN_EVENTS_FILE"
            and node.value is not None
        ]
        journal_source = source(commit, "src/server/journal.py")
        if not names:
            names = [
                ast.literal_eval(node.value.right)
                for node in ast.walk(ast.parse(journal_source))
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.BinOp)
                and any(
                    isinstance(target, ast.Name) and target.id == "events_path"
                    for target in node.targets
                )
            ]
        if len(names) != 1:
            message = "historical wire journal filename is ambiguous"
            raise ValueError(message)
        wire_filename = names[0]
        wire_event = {
            "sequence": 1,
            "run_id": "run-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "type": enum_value(wire_source, "RUN_STARTED", "EventType"),
            "data": {"kind": "run_started", "outer_loop": "single-agent", "input": "synthetic"},
        }
        if not set(wire_event) <= set(fields(wire_source, "RunEvent")):
            message = "wire event fields differ from historical source"
            raise ValueError(message)
        wire_payload = json.dumps(wire_event, sort_keys=True).encode() + b"\n"
        (DESTINATION / f"{name}.wire.jsonl").write_bytes(wire_payload)
        span_source = None if name == "pre-trace" else source(commit, SPANS)
        payload = journal(event_source, span_source, missing_identity=name == "missing-identity")
        (DESTINATION / f"{name}.jsonl").write_bytes(payload)
        sources = {
            EVENTS: event_source,
            "src/server/events.py": wire_source,
            "src/server/host_api.py": host_source,
            "src/server/journal.py": journal_source,
        }
        if span_source is not None:
            sources[SPANS] = span_source
        manifest[name] = {
            "events": [json.loads(line) for line in payload.splitlines()],
            "wire_event": wire_event,
            "commit": commit,
            "source_sha256": {
                path: hashlib.sha256(text.encode()).hexdigest() for path, text in sources.items()
            },
            "journal_sha256": hashlib.sha256(payload).hexdigest(),
            "wire_sha256": hashlib.sha256(wire_payload).hexdigest(),
            "wire_filename": wire_filename,
            "event_fields": fields(event_source, "CoreEvent"),
        }
    (DESTINATION / "history.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
