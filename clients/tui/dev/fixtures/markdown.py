#!/usr/bin/env python3
"""Generates `markdown.jsonl`, a synthetic run that exercises markdown rendering.

Recorded runs are the right fixture for tool and lifecycle rendering, but none
of the ones we have contain a table or a fenced code block, so they cannot
reproduce table and code-block bugs. This writes a small run whose assistant
turns are chosen to cover the markdown constructs the transcript claims to
support.

    python3 clients/tui/dev/fixtures/markdown.py

The output is checked in as plain JSONL rather than gzipped: it is small, and a
synthetic fixture is worth reading in a diff.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

RUN_ID = "markdown-fixture"
EXECUTION_ID = "inv-1"
ROUND_LABEL = "round-1-retry-1-implementer"
AGENT_KIND = "implementer"
START = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

NARROW_TABLE = """\
Benchmark results:

| Benchmark | Baseline | Candidate | Delta |
| --- | ---: | ---: | ---: |
| bfs | 412.7 | 301.4 | -27.0% |
| pagerank | 1180.2 | 1104.9 | -6.4% |
| sssp | 88.1 | 91.3 | +3.6% |

The ring buffer wins on `bfs` and loses slightly on `sssp`.
"""

WIDE_TABLE = """\
### Per-capacity throughput

| Capacity | Value size | Producer ns/op | Consumer ns/op | Throughput (Mops/s) | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | 8 | 41.2 | 39.8 | 24.1 | contended, expected |
| 2 | 8 | 22.7 | 21.9 | 44.9 | |
| 1024 | 8 | 6.1 | 5.9 | 168.3 | best case for the SPSC ring |
| 1024 | 4096 | 214.6 | 208.1 | 4.8 | memcpy dominates at this size |
"""

CODE_BLOCK = """\
Here is the hot loop after the change:

```rust
#[inline(always)]
fn push(&self, value: T) -> Result<(), T> {
    let head = self.head.load(Ordering::Relaxed);
    let next = (head + 1) & self.mask;
    if next == self.tail.load(Ordering::Acquire) {
        return Err(value);
    }
    unsafe { self.slots[head].get().write(MaybeUninit::new(value)) };
    self.head.store(next, Ordering::Release);
    Ok(())
}
```

And the shell that verifies it:

```bash
cargo build --release
./bin/tests && ./bin/bench --scale 1.0 --json /tmp/out.json
```

And the equivalent in the TypeScript harness the dashboard runs against:

```typescript
const CAPACITY = 1024;

function push<T>(ring: Ring<T>, value: T): boolean {
  const next = (ring.head + 1) & ring.mask;
  if (next === ring.tail) {
    console.warn("ring buffer full"); // never grows past CAPACITY
    return false;
  }
  ring.slots[ring.head] = value;
  ring.head = next;
  return true;
}
```
"""

MIXED = """\
## Plan

1. Replace the `Mutex<VecDeque<_>>` with a bounded SPSC ring.
2. Keep the C ABI **byte-identical** so the harness needs no changes.
3. Re-run the correctness gate before measuring.

> The checksum gate is the thing that makes this safe to do quickly.

Constraints that still apply:

- `sum=0x...` must not change
- `./bin/tests` must stay green
- no new dependencies

See [the objective](./OBJECTIVE.md) for the full contract.
"""

INLINE = (
    "Short answer: yes. The `head`/`tail` pair is *false-shared* today, and "
    "moving them onto separate cache lines is **the** win. No table here, no "
    "code fence, just inline markers."
)

PERF_METRIC = 562.9504
PERF_UNIT = "ms"

EXECUTION_STARTED = {
    "kind": "agent_execution_started",
    "stage": AGENT_KIND,
    "attempt": 1,
    "system_prompt": (
        "You are the Implementer. Own the active hypothesis: read the plan, "
        "change the candidate, and report what you measured. Return only the "
        "schema-valid JSON object.\n"
    ),
    "user_prompt": "Work the active hypothesis and return only the JSON object.",
    "activity": {
        "kind": "agent_execution_activity_changed",
        "mode": "thinking",
        "summary": "Implementing",
        "tool": None,
    },
    "driver": "agentshim",
    "provider": "claude",
    "model": "claude-opus-5",
}

EXECUTION_FINISHED = {
    "kind": "agent_execution_finished",
    "result": {
        "summary": (
            "Replaced the mutex-guarded VecDeque with a bounded SPSC ring over a "
            "preallocated slab, and moved the two indices onto separate cache lines."
        ),
        "expected_behavior": (
            "`./bin/tests` stays green and `./bin/bench --scale 1.0` reports a lower "
            "total_ms with every checksum unchanged."
        ),
        "hypothesis_outcome": "nominated",
        "perf_metric": PERF_METRIC,
        "perf_unit": PERF_UNIT,
    },
    "error": None,
}


def event(  # noqa: PLR0913
    sequence: int,
    offset_ms: int,
    kind: str,
    *,
    data: dict | None = None,
    status: str | None = None,
    round_label: str | None = None,
    agent_kind: str | None = None,
) -> dict:
    """Builds one on-wire RunEvent record."""
    return {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": RUN_ID,
        "timestamp": (START + timedelta(milliseconds=offset_ms)).isoformat().replace("+00:00", "Z"),
        "type": kind,
        "text": "",
        "diagnostic": None,
        "status": status,
        "round_label": round_label,
        "agent_kind": agent_kind,
        "invocation_id": None if agent_kind is None else EXECUTION_ID,
        "execution_id": None if agent_kind is None else EXECUTION_ID,
        "chat_thread_id": None,
        "data": data,
    }


def chunk(sequence: int, offset_ms: int, content: str, channel: str = "assistant") -> dict:
    """Builds one assistant output chunk, the events markdown is rendered from."""
    return event(
        sequence,
        offset_ms,
        "agent_output_chunk",
        data={
            "kind": "agent_output_chunk",
            "channel": channel,
            "content": content,
            "status": None,
        },
        status="active",
        round_label=ROUND_LABEL,
        agent_kind=AGENT_KIND,
    )


def build() -> list[dict]:
    """Returns the whole synthetic run, in sequence order."""
    events: list[dict] = [
        event(1, 0, "server_started", status="active"),
        event(
            2,
            10,
            "server_ready",
            data={"kind": "server_ready", "socket_protocol": "jsonl"},
            status="active",
        ),
        event(
            3,
            20,
            "run_started",
            data={
                "kind": "run_started",
                "outer_loop": "agent",
                "input": "markdown",
                "max_rounds": 1,
                "expected_roles": ["orchestrator", "implementer", "judge", "profiler"],
            },
            status="active",
        ),
        # A real run opens the execution with its own lifecycle event and
        # only then reports the phase, so this fixture records both, in that
        # order. The pair is load-bearing rather than decorative: for an
        # execution that records no lifecycle event of either spelling, the
        # server's read path synthesizes one from each phase event, at a
        # sequence the phase event already uses, and `journal.ts` does not port
        # that branch.
        event(
            4,
            25,
            "agent_execution_started",
            data=EXECUTION_STARTED,
            status="active",
            round_label=ROUND_LABEL,
            agent_kind=AGENT_KIND,
        ),
        event(
            5,
            30,
            "phase_started",
            data={"kind": "phase", "phase": "implement", "attempt": 1},
            status="active",
            round_label=ROUND_LABEL,
            agent_kind=AGENT_KIND,
        ),
    ]

    sequence = 6
    offset = 100
    # Each of these arrives as one complete chunk, which is the case that must
    # render correctly the instant it lands. The transcript concatenates
    # consecutive chunks of one turn, so they carry their own trailing blank
    # line exactly as a real agent's output does; without it the next block's
    # leading marker would land mid-line and stop being a marker at all.
    for body in (INLINE, MIXED, NARROW_TABLE, WIDE_TABLE, CODE_BLOCK):
        events.append(chunk(sequence, offset, body.rstrip("\n") + "\n\n"))
        sequence += 1
        offset += 400

    # The same table again, split mid-row across chunks. The transcript
    # concatenates streaming chunks, so this is what a table looks like while it
    # is still arriving, and what it must look like once it has stopped.
    streamed = NARROW_TABLE.replace("Benchmark results:", "Streamed table:")
    for piece in (streamed[:90], streamed[90:180], streamed[180:]):
        events.append(chunk(sequence, offset, piece))
        sequence += 1
        offset += 250

    events.extend(
        [
            event(
                sequence,
                offset + 300,
                "agent_execution_finished",
                data=EXECUTION_FINISHED,
                status="completed",
                round_label=ROUND_LABEL,
                agent_kind=AGENT_KIND,
            ),
            event(
                sequence + 1,
                offset + 400,
                "phase_finished",
                data={"kind": "phase", "phase": "implement", "attempt": 1},
                status="completed",
                round_label=ROUND_LABEL,
                agent_kind=AGENT_KIND,
            ),
            event(
                sequence + 2,
                offset + 500,
                "round_finished",
                data={
                    "kind": "round_finished",
                    "attempts": 1,
                    "judge_verdict": "pass",
                    "perf_metric": PERF_METRIC,
                    "perf_unit": PERF_UNIT,
                    "profile_skipped": False,
                },
                status="completed",
                round_label=ROUND_LABEL,
            ),
            event(sequence + 3, offset + 600, "run_finished", status="completed"),
        ]
    )
    return events


def main() -> None:
    """Writes markdown.jsonl beside this script."""
    target = Path(__file__).with_name("markdown.jsonl")
    lines = [json.dumps(item, separators=(",", ":")) for item in build()]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} events to {target}")  # noqa: T201


if __name__ == "__main__":
    main()
