# TUI development harness

Runs the real TUI against a recorded run, with no Python backend, no agent, and
no tokens. Use it to iterate on rendering, layout, themes, and keybindings.

Nothing here ships. `tsconfig.json` sets `rootDir: "src"`, so `dev/` is never
compiled into `dist`, and `package.json` publishes only `dist`. No file on the
shipping path imports anything in this directory. `tsconfig.check.json` widens
the root to cover `dev/**`, so `pnpm check:clients` typechecks this code; that
pass emits nothing, so it cannot put any of it on the shipping path.

That widening has one cost: with `dev/` in the check program, a `src/` file
importing this directory typechecks cleanly, where before it did not. The build
still rejects it (`rootDir: "src"`, TS6059), but the rule belongs where the other
layering rules live, so `.dependency-cruiser.cjs` states it directly as
`shipping-path-does-not-depend-on-dev-harness`.

## Why it needs no product code

The TUI is already a separate process that connects to whatever Unix socket
`VIBESYS_CONTROL_SOCKET` names (`src/index.ts`). `mock-server.ts` speaks the
server side of that protocol and replays a recorded `run-events.jsonl`. The
binary under test is the unmodified `dist/index.js`.

## Usage

```bash
pnpm --dir clients/tui build          # once, and after any src change
clients/tui/dev/mock-ui.sh            # replay the bundled fixture
```

| Flag | Effect |
| --- | --- |
| `--speed N` | Wall-clock divisor from the recorded timestamps. `0` delivers everything at once. Default `8`. |
| `--max-gap MS` | Caps any single gap, so a four-minute agent turn does not stall the replay. Default `400`. |
| `--paused` | Hold before the first live event; `/resume` in the TUI starts it. |
| `--bootstrap N` | Deliver the first N events instantly as recorded history, then stream the rest. |
| `--theme NAME` | Any theme in `src/ui/theme.ts`. |
| `--fixture PATH` | Any `run-events.jsonl`, plain or `.gz`, including one of your own runs. |
| `--tmux COLSxROWS` | Run detached in tmux for scripted frame capture. |

`/pause` and `/resume` inside the TUI control the replay itself, so you can stop
on a frame and inspect it.

### Process lifetime

`mock-ui.sh` runs both halves under one process: it starts the server, waits for
the bind, then `exec`s the client over its own shell. Nothing is left outside to
clean up afterwards, so the server ends itself. It exits when its last
subscriber disconnects, on `SIGHUP`, `SIGINT`, or `SIGTERM`, and when the
`--owner-pid` it was given stops existing. That last one covers the window the
subscription watch cannot see: a client that dies between the bind and its
first `subscribe`, on a missing runtime or an exception during initialisation,
never reaches `onLastSubscriberGone` and used to leave the server adopted by
PID 1 with its socket and log still on disk.

There is deliberately no loop flag. Restarting the fixture would replay
`run_started` into a client that already folded the run to terminal, leaving it
with a running status, a terminal flag still set, and a second transcript
appended to the first. Looping needs a real reset of client state, which the
protocol has no message for, so the honest options were to remove it or to fake
a reconnect. Re-run the command instead.

### Scripted capture

```bash
clients/tui/dev/mock-ui.sh --tmux 200x50 --speed 0
tmux capture-pane -t vsmock -p        # plain text, for layout and wrapping
tmux capture-pane -t vsmock -e -p     # with escapes, for color and contrast
tmux send-keys -t vsmock Enter
tmux kill-session -t vsmock
```

### Replaying your own run

```bash
clients/tui/dev/mock-ui.sh --fixture ~/.vibesys/projects/<project>/runs/<run>/logs/run-events.jsonl
```

## Fixtures

Both are real captures, scrubbed of local usernames and home paths, committed
plain. Git zlib-compresses blobs, so 1.7MB of JSONL costs a packed repository
the same ~266KB either way; gzipping would only make the scrub unreviewable and
a hand-edit unreadable as a diff. They are deliberately not marked `-diff`:
that hides the scrub in review, which is the reason they are plain at all. If a
re-record ever makes the diff noise a real problem, mark them then.

| Fixture | Events | Why it exists |
| --- | --- | --- |
| `bad-cpp-round1.jsonl` | 403 | The only capture with a full lifecycle: `run_started` through `judge_result`, `benchmark_result`, `round_finished`, `run_finished`, plus four `chat` turns. |
| `queue-rs-payloads.jsonl` | 628 | Every one of its 105 `tool_result` events carries a typed `payload` (`kind: "command"`), and it has 214 `agent_execution_activity_changed` events. This is the one to use for tool-result rendering work. It ends in `run_interrupted`/`run_failed`, so it also exercises the error banner. |
| `framework-events.jsonl` | 18 | Synthetic (see below). The #692 typed framework events: gate pairs including a reused pass and a failure with `output_tail`, a benchmark measurement, all three `workspace_snapshot` aspects, `run_configured`, and a `framework_warning` with its lifted diagnostic. |

Neither carries `todo_update` events, so the todo strip stays empty on both.

`bad-cpp` predates the current schema: its records omit `execution_id`,
`chat_thread_id`, and `diagnostic`, and every `tool_result` has `payload: null`.
`RunEvent` tolerates that by design, so it is the harness's one capture of a
client falling back to raw `content`.

That tolerance is checked rather than assumed. `tests/server/test_tui_dev_harness.py`
declares what each fixture guarantees: every line of every fixture must validate
as a `RunEvent`, and `queue-rs-payloads`, recorded on the current schema, must
additionally re-serialize to itself byte for byte, which is what catches a field
being added. `bad-cpp-round1` is declared legacy there and only has to validate.
A new fixture has to declare the same, or the directory check fails.

### `markdown.jsonl`

Synthetic, and generated by `markdown.py` rather than recorded: no real capture
we have contains a markdown table or a fenced code block, so none of them can
reproduce table or code-block rendering bugs. Regenerate with:

```bash
python3 clients/tui/dev/fixtures/markdown.py
```

It covers inline markers, headings, ordered and unordered lists, a blockquote, a
link, a narrow table, a table wide enough to need shrinking inside a card, two
fenced code blocks, and the same table again split mid-row across three chunks
so the streaming path is exercised too.

### `framework-events.jsonl`

Synthetic, written through the Python `RunEvent` model (which is what makes its
declared byte-for-byte round trip hold): no real capture yet carries the typed
framework events of #692. One run, two rounds: round 1 passes two validation
recipes (one reused), accuracy, and a benchmark gate with a
`tok_per_sec` measurement; round 2 fails accuracy with an `output_tail` and
raises a `framework_warning` whose diagnostic (severity `warning`, source
`loop`) rides the envelope. Around them sit `run_configured` and all three
`workspace_snapshot` aspects: baseline, exclusions, and snapshots with and
without a commit.

### Legacy translation

A legacy capture still replays with its agent executions, because the harness
applies the same translation the real read path applies. `journal.ts` ports it:
`execution_id` is recovered from the legacy `invocation_id`, and
`invocation_started`/`invocation_finished` are rewritten into
`agent_execution_started`/`agent_execution_finished`, with the opening activity
synthesized from the agent kind the way `EventJournal` synthesizes it. A journal
that recorded both spellings of a boundary, which `queue-rs-payloads` does,
delivers the canonical one and drops the legacy one, so 628 recorded events
reach the client as 622.

The port cannot import the Python it copies, so the two are pinned to one file:
`test_canonical_events_match_the_backend_read_path` reads every fixture through
a real `EventJournal` and writes `canonical-events.golden.json`, and the parity
test in `harness.test.ts` holds `journal.ts` to it. Regenerate the golden with
`UPDATE_CANONICAL_EVENTS=1 uv run pytest tests/server/test_tui_dev_harness.py`
and read the diff as a backend change the harness has not been taught yet.

One branch of the Python is not ported: the one that synthesizes a lifecycle
event from `phase_started`/`phase_finished` for an execution that recorded no
lifecycle event at all. No fixture reaches it, the synthetic one included: a
generated fixture records the lifecycle pair a real run records. It is the only
branch that emits a second event at an already-used sequence, which the
replay's one-event-per-sequence stepping does not model.

## Protocol notes

Relevant if you extend `mock-server.ts`:

- Newline-delimited JSON both directions, no handshake. The client says nothing
  on connect and correlates purely by `request_id`.
- Every response needs `protocol_version: 1`, `request_id`, and `ok`, or the
  client destroys the socket and fails every pending request.
- The TUI opens three connections to the one path: control, the event stream
  (one per `subscribe`), and one per `query.chat`. Connections are handled
  independently; none is closed early.
- On `subscribe`, order is `subscribed`, then a bootstrap `event_batch`, then
  live batches. `history_after_sequence: 0` tells the TUI it holds the whole
  history and suppresses backfill requests.
- The recorded `run-events.jsonl` line format is exactly the `RunEvent` that
  goes on the wire, so replay is re-enveloping lines into `event_batch`, after
  the legacy translation above. Re-enveloping alone was wrong for a legacy
  capture: the server never serves one raw, and a client that receives one folds
  no agent executions out of it.
- A snapshot and every `event_batch` carry `active_executions`, the server's
  liveness checkpoint. The mock derives it from the events it has delivered
  rather than keeping a second description of the same fact. Answered from the
  static body instead, it was an empty checkpoint at the sequence the bootstrap
  had just reached, and `reduceSnapshot` accepts one at an equal sequence, so a
  snapshot landing after the batch erased the execution the batch had opened.
- Response bodies are literals in `mock-responses.json`, keyed by request type,
  rather than inline in `mock-server.ts`. That is what lets
  `tests/server/test_tui_dev_harness.py` validate the exact bytes sent against
  the `Response` model. Runtime values go in through `withValues`, which
  refuses any key the file does not already carry.

## Tests

`harness.test.ts` covers the parts that only exist at runtime: process
lifetime, by driving `mock-ui.sh` with a client that exits before it
subscribes; the liveness checkpoint, by folding a real bootstrap batch plus the
snapshot query that follows it through `@vibesys/core-state`; and the legacy
translation, by replaying `bad-cpp-round1` over a real socket and folding the
executions back out with the client's own reducer. It runs with the TUI's own
suite (`pnpm --dir clients/tui test`). The fixtures, the static response bodies,
and the canonicalization golden are checked separately, in
`tests/server/test_tui_dev_harness.py`.
