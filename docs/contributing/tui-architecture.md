# TUI architecture

The Python packages follow one inward dependency direction:

```text
entrypoints -> server -> vibesys
```

`vibesys` is the headless optimization library. It owns run execution and the
durable core event journal, and does not import serving or process-entrypoint
code. `server` imports the core in-process, projects core events into its wire
journal, and owns frontend-specific facilities such as experiment chat.
`entrypoints` composes either a local headless integration or the server
runtime. Tach enforces this direction in CI.

The TypeScript frontend has four packages with one allowed dependency direction:

```text
@vibesys/backend-client <- @vibesys/core-state <- @vibesys/tui
                         \____________________<- @vibesys/web
```

`@vibesys/tui` and `@vibesys/web` may depend on the lower layers. They never depend on each other,
and reverse imports and cross-package relative imports are forbidden and checked by
`pnpm check:ts-architecture`.

Dependency-cruiser parses and resolves the TypeScript graph for dependency direction, cycles,
unresolvable or undeclared imports, public package entry points, and the runtime-independence rules
for `core-state`. `tsconfig.architecture.json` maps workspace package names to their public source
entry points, so the check does not depend on prior builds. A small manifest check covers forbidden
workspace dependencies that are declared but unused, because they do not appear in a source
dependency graph. Rule regressions run as part of `pnpm test:clients`.

The check scans each package's `src/` plus the non-shipping code next to it (`tui/dev`, benchmarks,
the web end-to-end tests, and `clients/scripts`). Beyond the package direction, it enforces:

- No deep imports into another workspace package (`@vibesys/x/dist/...`, `@vibesys/x/src/...`,
  relative paths into a sibling package). Only the public `exports` are importable.
- Nothing in a package imports `tui/dev`, benchmarks, or `scripts`; the tooling itself must still
  resolve, declare its dependencies, and stay free of cycles.
- Inside `tui/src`: OpenTUI is confined to `ui/` and the composition root (`index.ts`,
  `runtime.ts`); state and controller modules do not import the controller or the wiring above them;
  `ui/` reaches `session-controller` by type only and never imports the composition root;
  `index.ts` and `launcher.ts` are never imported; `launcher.ts` imports nothing but `ui/theme.ts`.
  Test files are exempt from the `tui/src` layer rules.

## Ownership

| State or behavior | Owner |
| --- | --- |
| Generated protocol types, socket framing, connection lifecycle, requests, event subscription | `backend-client` |
| Status, rounds, phases, executions, transcripts, todos, usage, benchmarks, diagnostics | `core-state` |
| Focus, selection, layout, zoom, theme, modals, drafts, query progress | `tui` |
| Terminal widgets, rendering, keyboard and mouse events | `tui` |
| Browser bindings, presentation, and browser-only interaction state | `web` |

The backend client performs I/O and exposes validated protocol messages. Core state is a pure fold
over snapshots, ordered events, and active-execution checkpoints. The TUI owns all interaction and
presentation state, renders the combined state, and sends user intents through the backend client.

Only backend messages change core state. A frontend action may send a command, but the command does
not optimistically change backend-authoritative state. The resulting backend event does.

The web client is a presentation adapter over `core-state`. Its React external-store binding owns
subscriptions and browser presentation state; it does not fold events or copy TUI state logic.
Recorded replay fixtures are served by the development harness and folded through the same
`core-state` reducer used by live clients.

`core-state` has no Node runtime, OpenTUI, theme, layout, focus, or query-result dependencies. Its
time-dependent selectors require an explicit clock value so tests remain deterministic. Transcript
labels and tones are semantic annotations derived from event fields; the TUI decides whether and how
to display them.

Experiment entries currently come from `query.experiments`. The event stream supplies an
`experiments_changed` invalidation, not the entries themselves. Query progress and results therefore
remain outside core state until the backend event contract becomes complete enough to project them.

## Launch sequence

`vibesys` spawns the server and the frontend concurrently. The launcher does not wait for the
control socket: the backend client retries `ENOENT` and `ECONNREFUSED` until its connect deadline,
so the frontend pays its own startup while the backend is still coming up. The launcher still
watches for the socket appearing, which is what distinguishes a backend that died before it ever
listened (report its log tail) from a run that failed later (the frontend already shows the
diagnostic).

Configuration stays in the backend. Without `--theme`, the frontend asks `query.tui_defaults` while
the renderer starts and applies the answer before the first frame, falling back to the default theme
if the backend does not answer in time. `--theme` skips the query and reaches the frontend as
`VIBESYS_THEME`.

### Boot trace

Boot timings are always recorded and never narrated. The backend times its boot in spans
(`src/vibesys/boot_trace.py`): the dispatch preamble in `src/entrypoints/headless.py`, then
run-context assembly in
`context.py`. Every span lands in the run's `run-*.log` as
`boot span <qualified.name>: <ms>ms`, with the preamble's spans ahead of assembly's and each
enclosing span reporting its region's total after its children.

Nothing reaches stderr unless you ask:

```bash
VIBESYS_BOOT_TRACE=1 vibesys --input ... 2>trace.log
```

The CLI passes the request to every process it spawns, so the same variable also switches on the
frontend's own measurement of how long the landing view waits for experiments
(`clients/tui/src/boot-trace.ts`), which spans the request, the backend gate, and the reply. Those
client lines are anchored to `VIBESYS_LAUNCH_START_MS`, which the CLI always sets, so they report
wall time since the user ran the command rather than since the frontend process started.

## Validation

Run all package checks from `clients/`, the TypeScript workspace root:

```bash
cd clients
pnpm check:ts-architecture
pnpm check:knip
pnpm check:clients
pnpm test:clients
pnpm build:clients
```

`pnpm check:knip` (knip, configured in `clients/knip.jsonc`) fails on unused files, exports,
dependencies, and unlisted or unresolved imports. Entry points are the package `bin` and `exports`
plus the declared test, harness, and benchmark files; an export used nowhere in the workspace should
lose its `export` or be deleted. `backend-client/src/generated/` is ignored because the generator
exports every schema type, and the `index.ts` of each library package is its public API. Add an
entry point to `knip.jsonc` (with a comment saying who runs it) rather than suppressing a finding.

Each package also supports its own `check`, `test`, and `build` scripts. Package builds consume only
public workspace exports. The release build uses the same dependency-aware build chain before pnpm
deploys the self-contained TUI payload.

### Regression tests for rendering bugs

Pick the test layer by where the symptom lives, and require that the test fails at the merge base:

| Symptom | Layer |
| --- | --- |
| String formatting or truncation arithmetic | Pure function test on the formatter |
| Width, alignment, clipping, padding, or overlap (anything stated in columns or rows) | `createTestRenderer` from `@opentui/core/testing`: build the real view, render at a fixed size, and measure the emitted renderables (see `clients/tui/src/ui/app.test.ts` and `todo-strip.test.ts`) |
| Theme contrast and legibility | Pure computation over theme tokens |
| Wide characters, resize, streaming timing, keybindings through a PTY | The tmux harness in the `tui-bug-hunt` skill; not a CI regression test |

The test renderer runs the real renderable tree and Yoga layout, so it reproduces layout-class bugs
deterministically in CI. It does not run a terminal: text is measured in code units, and there is
no PTY, input, or timing. A pure-function test that computes the expected width itself cannot
reproduce a layout bug and is not accepted as evidence for one.

To exercise rendering by hand without a backend, a provider, or tokens, replay a recorded run through
the development harness described in
[`clients/tui/dev/README.md`](https://github.com/uw-syfi/vibesys/blob/main/clients/tui/dev/README.md).
The harness applies the same legacy translation the server's read path applies, so a capture recorded
before `execution_id` existed replays with its agent executions intact, as a client would receive it.
