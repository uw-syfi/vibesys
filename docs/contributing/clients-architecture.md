# Clients architecture: TypeScript package graph

This document owns the structure of the TypeScript clients under `clients/`: which packages exist,
which way they may depend on each other, what each entry point may use from its runtime, and how
the workspace builds, checks, and tests them. It is the design of record. Packages that do not
exist yet are marked in the [migration status](#migration-status) table, with the item that
creates each one.

It builds on, and does not restate:

- [`architecture.md`](architecture.md): the Python module graph and the placement rule. The same
  principles (deep modules, one-way dependencies, a pure core with a thin shell) apply here.
- [`desktop-connection.md`](desktop-connection.md): what the desktop main process, the host helper,
  and the screen stores do. This document says which package each of those lives in.
- [`wire-protocol.md`](wire-protocol.md): the run protocol that `@vibesys/contracts` types.
- [`web-development.md`](web-development.md): how to run the web UI and the desktop app.

`clients/tui` is deprecated. It keeps building and its checks stay green, but it is not migrated
into this graph; it consumes the packages below through their public entry points like any other
app, and is deleted when the desktop app replaces it.

## Why

`clients/web` and `clients/desktop` grew as two large packages with layering rules enforced inside
each (`state/` pure, `ui/` free of I/O, `effects/` never importing `ui/`). Those rules hold inside a
package but say nothing about which feature may reach which, or which runtime a module may use.
A feature that imports another feature's internals, or renderer code that imports a Node module,
type-checks today and is caught, if at all, by review.

The fix is the same one the Python side uses with `tach`: make every boundary a package boundary,
declare the edges, and let tooling refuse the rest. A package is used only through its `exports`
map; each entry point is compiled with exactly the runtime library it may use; and one checker
validates the graph from the manifests. Per-feature packages make each feature small enough to
read whole, and the build model below keeps a graph of thirty packages as fast as the current five.

## The package graph

```text
clients/
  libs/contracts/   @vibesys/contracts        generated message types + the one runtime validator
  libs/ui/          @vibesys/ui               domain-free React primitives + design tokens
  libs/store/       @vibesys/store            domain-free reducer kernel + fold model-test kit
  libs/machine/     @vibesys/machine          domain-free machine glue + machine model-test kit
                                                                                 -> store
  libs/run-state/   @vibesys/run-state        pure fold of run events; ./rounds, ./performance,
                                              ./transcript projections -> contracts
  libs/transport/   @vibesys/transport        framing, redial, persistent stream -> contracts
  libs/hosts/       @vibesys/hosts            host identity, listing, connection status,
                                              checkout path rules              -> contracts
  libs/runs/        @vibesys/runs             run identity, recent status, resume and stop
                                                                                 -> contracts, hosts
  libs/run-session/ @vibesys/run-session      one live run: runStep, the session machine, its
                                              ports, connectRun, WebSession    -> contracts, run-state,
                                                                       transport, store, machine
  libs/transport-node/ @vibesys/transport-node   stdio / unix socket / ssh channel  -> transport
  libs/ipc/         @vibesys/ipc              typed main <-> renderer messages, the welcome
                                              requests' parsers per feature      -> hosts, runs
  libs/connection/  @vibesys/connection       host connection manager            -> contracts, transport, hosts,
                                                                                    machine
  libs/settings/    @vibesys/settings         settings store, atomic files       -> hosts, runs
  features/<x>/     @vibesys/feature-<x>      run-view, gateway-connect, replay, hosts,
                                              start-run, run-control, recent-runs
  apps/desktop-main      @vibesys/app-desktop-main      composition only (Electron main), and the
                                                        sandboxed preloads
  apps/desktop-renderer  @vibesys/app-desktop-renderer  composition only (Electron renderer)
  apps/web               @vibesys/app-web               composition only (browser, and the
                                                        desktop shell's run page)
  tui/              @vibesys/tui              deprecated terminal UI; stays at the root until it is deleted
  tools/            scripts/ (checkers, scaffold), lint-rules/ (Biome plugins), and the shared
                    tsconfig.base.json, tsconfig.architecture.json, knip.config.ts
```

The folder a package sits in mirrors its layer: `libs/` holds the `foundation`, `core`, `domain`,
and `service` packages, `features/` the `feature` packages, and `apps/` the `app` packages. The
manifest's `vibesys.layer` stays the one source of truth; the `layer-matches-folder` rule fails a
package filed under the wrong folder. Imports are by package name, so moving a package between
folders changes no source. `tui` is the one package outside the three folders (see
[Layers](#layers)). Files a tool requires at the workspace root stay there: `package.json`,
`pnpm-workspace.yaml`, `pnpm-lock.yaml`, `turbo.json`, `biome.json` and `biome.warnings.json`
(Biome resolves a config's plugin paths against the config that extends it, so the warnings
config sits beside `biome.json`), and `.dependency-cruiser.mjs`.

```mermaid
flowchart BT
  contracts["contracts (L0)"]
  ui["ui (L0, DOM)"]
  store["store (L0)"]
  run-state["run-state (L1)"] --> contracts
  transport["transport (L1)"] --> contracts
  hosts["hosts (L1)"] --> contracts
  runs["runs (L2)"] --> contracts & hosts
  machine["machine (L1)"] --> store
  run-session["run-session (L2)"] --> contracts & run-state & transport & store & machine
  transport-node["transport-node (L2, Node)"] --> transport
  ipc["ipc (L3)"] --> hosts & runs
  connection["connection (L3, Node)"] --> contracts & transport & hosts & machine
  settings["settings (L3, Node)"] --> hosts & runs
  features["feature-* (L4)"] --> run-session & runs & hosts & ipc & ui & store
  features -. "./main only" .-> connection & settings
  apps["apps/* (L5)"] --> features
```

### Layers

Every package declares its layer in `package.json` (`"vibesys": {"layer": ...}`). A package may
depend only on packages in a strictly lower layer. That one rule gives the direction, rules out
cycles, and forbids feature-to-feature and app-to-app edges, because peers share a layer.

| Layer | Rank | Packages | May depend on |
| --- | --- | --- | --- |
| `foundation` | 0 | `contracts`, `ui`, `store` | nothing in the workspace |
| `core` | 1 | `run-state`, `transport`, `hosts`, `machine` | foundation |
| `domain` | 2 | `runs`, `run-session`, `transport-node` | foundation, core |
| `service` | 3 | `ipc`, `connection`, `settings` | foundation, core, domain |
| `feature` | 4 | `feature-*` | anything below |
| `app` | 5 | `apps/*` (and the deprecated `tui`) | anything below |

This refines the sketch in the brief in three places. `ui` and `store` sit in `foundation`: rank 0
is the mechanical form of "no domain dependencies", so the layer rule enforces rule 6 for them with
no special case. `transport-node`, `connection`, and `settings` get ranks of their own (`domain` and
`service`) because each depends on another package the sketch places at the same level
(`transport-node` on `transport`, `connection` on `transport`, `hosts`, and `machine`, `settings` and `ipc` on
`hosts` and `runs`). And `hosts` sits in
`core`, one rank below `runs`, because a run names the host it runs on (a recent run's status reads
its host's listing; the stop flow is keyed by host) while a host never names a run's model.

### Packages are named for one concept

A package is named for the concept it owns, never for a place to put things. `@vibesys/core-state`
and `@vibesys/entities` were split for this reason: the first held both the generic store kernel
and the run fold, the second every domain model the views shared, and each name invited the next
unrelated addition. The split gave `store` (generic, domain-free like `ui`), `run-state` (the run
fold, its round, performance, and transcript projections as separate entries), `hosts`, `runs`, and
`run-session` (the live session below the UI, out of `feature-run-view`, which keeps only its
interaction state, banner copy, and UI). A helper used by one feature lives in that feature
(`shellWords` is `@vibesys/feature-start-run/state`'s). The `bucket-names` rule keeps foundation,
core, and domain packages from being named `entities`, `core`, `common`, `utils`, `shared`,
`helpers`, or `misc`.

### Entry points and runtimes

Every entry in a package's `exports` map declares the runtime it may use, in the same
`"vibesys"` block:

| Runtime | Compiler `lib` | Compiler `types` | Who may import it |
| --- | --- | --- | --- |
| `neutral` | `ES2022` | none | any entry |
| `dom` | `ES2022`, `DOM`, `DOM.Iterable` | none (`vite/client` for apps) | `dom` entries |
| `node` | `ES2022` | `node` (and `electron` where needed) | `node` entries |
| `preload` | `ES2022`, `DOM` | `node` (Electron's renderer API types need Node's) | `preload` entries |

A `preload` is an Electron sandboxed preload: it runs in a page's renderer process before the page,
with the DOM and Electron's renderer APIs. The sandbox gives it a `require` that reaches `electron`
and nothing else, so it may import a workspace package only as a type (`import type`,
`typeof import(...)`, which the compiler erases), no Node builtin, and no `node` entry. Each preload
is one file and one entry, which the main process loads by path into a page.

An entry may import an entry of another package only when the target is `neutral` or has the same
runtime. A feature package has up to three entries:

- `./state` (`neutral`): the pure reducer, selectors, and the interfaces its effects need (for
  example `RunControl`). No React, no I/O.
- `./renderer` (`dom`): state plus `ui/` components plus `effects/`, which call the injected
  interfaces. It depends on `@vibesys/ui`, never on a platform package.
- `./main` (`node`): the main-process controller that answers the feature's requests with
  `@vibesys/connection` and `@vibesys/settings` (`Hosts`, `RunControl`). `apps/desktop-main` routes
  each request `@vibesys/ipc` parsed to the controller of the feature that owns it.

A feature may also publish `./styles.css`, the stylesheet of the views it renders (a `dom` asset,
like `@vibesys/ui`'s `./tokens.css`); an app imports the stylesheets of the features it composes and
keeps only its own layout in its stylesheet, so a feature's look moves with the feature.

A feature publishes only the entries it has. Its pure core is `./state` when it needs nothing
beyond `ES2022`; a core that needs a DOM global (`@vibesys/feature-gateway-connect` parses with
the WHATWG `URL`) stays pure but lives in `renderer/state/`, and the feature publishes `./renderer`
alone. Effects reach the platform only through the interfaces `./state` declares, which the app
implements. A domain package that owns effects declares its ports the same way:
`@vibesys/run-session` declares `RunConnection`'s control transport and `BrowserLifecycle`, and
`apps/web` implements them with its backend adapter and lifecycle.

Node-only packages (`transport-node`, `connection`, `settings`) therefore reach the renderer
through no path: only `node` entries may import them, and only `desktop-main` and features'
`./main` entries are `node`. A preload is not `node` either, so it reaches none of them.

### Rules and what enforces them

| # | Rule | Enforced by (rule id in `check_package_graph.mjs`) |
| --- | --- | --- |
| 1 | Layer direction; Node-only packages reachable only from `node` entries | `layer-direction`, `runtime-reachability` |
| 2 | No feature depends on another feature; cross-feature flows are composed in apps | `layer-direction` (peers share a layer) |
| 3 | A package is used only through its `exports` map; every import of a workspace package is a declared dependency | `imports-use-exports`, `imports-are-declared`, `imports-are-workspace-packages`, `workspace-protocol`; `.dependency-cruiser.mjs` for relative paths into another package |
| 4 | Each entry is compiled with the `lib` and `types` of its runtime, so importing a Node module from a renderer entry is a type error | `entry-projects`, `entry-capability`; `tsc -b` then enforces it |
| 5 | Inside a package: `state/` pure, `ui/` free of I/O, `effects/` never import `ui/`, effects depend on injected interfaces | `tools/lint-rules/*.grit` (Biome) and `.dependency-cruiser.mjs` |
| 6 | `@vibesys/ui` and `@vibesys/store` have no domain dependencies and name no domain concept; helpers live in the lowest package that needs them, with no shared junk drawer | `layer-direction` (both are rank 0), `domain-free` (no word of `DOMAIN_WORDS`, or an inflection of one, in their production source or stylesheet); review |
| 7 | A foundation, core, or domain package is named for the concept it owns, not for a bucket | `bucket-names` (no segment of its name in `BUCKET_NAMES`) |
| 8 | A sandboxed preload loads only Electron at run time; every workspace import in it is erased | `preload-sandbox` (what the entry's emitted CommonJS still requires); `.dependency-cruiser.mjs` keeps a preload's imports to `@vibesys/ipc` and Electron |
| 9 | Control state is an XState machine; data folds are plain reducers (see [State](#state)) | `external-dependencies` (`xstate` only in features' `./state` and `./renderer` and `MACHINE_PACKAGES`); `tools/lint-rules/features-no-hand-rolled-control.grit`, `tools/lint-rules/control-state-no-store-kernel.grit` |
| 10 | A package's folder matches the layer its manifest declares: `libs/*` is foundation, core, domain, or service; `features/*` is feature; `apps/*` is app; nothing else, except the deprecated `tui` (`DEPRECATED_OUTSIDE_FOLDERS`) | `layer-matches-folder` |

All package-graph rules, and the tables they read, live in one module,
[`clients/tools/scripts/check_package_graph.mjs`](https://github.com/uw-syfi/vibesys/blob/main/clients/tools/scripts/check_package_graph.mjs),
run by `pnpm check:ts-architecture`. Its tests build a valid fixture workspace and break it once
per rule, and a meta-test fails when a rule has no failing fixture. `.dependency-cruiser.mjs` keeps
the rules about modules inside a package and about how imports resolve.

### How dependencies are managed

**One declaration.** A package's `package.json` is the single source of truth for its place in the
graph:

```json
"vibesys": {"layer": "feature", "entries": {"./state": "neutral", "./renderer": "dom", "./main": "node"}}
```

The checker, the scaffold (which writes one `tsconfig.<entry>.json` per entry from it), Knip's
policy, and this document all read that block. An export whose source is a stylesheet
(`@vibesys/ui`'s `./tokens.css`) is a `dom` asset: it has no tsconfig project, and Vite resolves
it through the same source condition. Unknown keys, layers, and runtimes are rejected by
name (`declarations`), and the entries must be exactly the `exports` keys
(`entries-match-exports`).

**Workspace dependencies** are declared as `workspace:*` in the section the code needs
(`dependencies` for production code; test code may also use `devDependencies`), point to a lower
layer, and are mirrored by tsconfig project `references`. The references must equal the declared
workspace dependencies, no more and no fewer (`references-match-dependencies`): a missing
reference is not a compiler error, because the import still resolves to the dependency's source.

**Third-party dependencies** are confined by runtime and layer through one allowlist table in the
checker (`EXTERNAL_DEPENDENCIES`, rule `external-dependencies`):

| Import | Allowed in |
| --- | --- |
| `react`, `react-dom` (and subpaths) | `dom` entries: `ui`, features' `./renderer`, browser and renderer apps |
| `electron` | `node` and `preload` entries of features (`./main`) and apps (`desktop-main`) |
| `@opentui/core` | `node` entries of apps (the deprecated `tui`) |
| `xstate` | features' `./state` and `./renderer`; `MACHINE_PACKAGES`: `machine`, `connection` (its host connection machine), and `run-session` (its session machine). Never `store`, `run-state`, `hosts`, `runs`, `ui`, `contracts`, `transport`, or `ipc` |
| Node builtins (`node:*` and bare names) | `node` entries |

A package that declares one of them in `dependencies` must have an entry allowed to use it. Knip
(`pnpm check:knip`) composes with this: it reports dependencies declared but never imported and
imports of packages not declared, so between the two a dependency is declared exactly when it is
used, and used only where its runtime allows.

**Legacy packages.** `tui` predates the per-entry layout, so `LEGACY_EXEMPTIONS` in the checker
lets it skip the per-entry project rules; every other rule applies to it. It is deprecated and keeps
its exemption until it is deleted. The table only shrinks: `desktop` left it when its main process
was composed from packages (`apps/desktop-main`, phase 8 of #1878).

## State

Client state comes in two kinds, and each has one form.

**Control state is an XState v5 machine.** Control state is a mode with effects: it starts work,
waits for it, retries, times out, cancels, and decides what a late answer means. The live run
session (start-up, then the stream; single-flight recovery), the run-control banner (the latest
command wins), the desktop connection manager (dial, back off, redial on wake), and a sheet that
loads, submits, and cancels are all control state. Each is written as
`setup({types, actors, actions, guards, delays}).createMachine(...)`:

- every operation is an invoked actor with an explicit `id`, declared with a placeholder
  (`unprovided` from `@vibesys/machine`) and provided by the shell with `machine.provide`, so the
  machine stays pure and the operation stays injectable;
- a delay is an `after` transition, with the clock injected through `createActor`'s `clock`
  option (`clockFrom` adapts a `ScheduleTimeout`, so tests drive it with a fake clock);
- cancellation is leaving the state that owns the work: the operation stops, its delay is
  cancelled, and a stopped operation never reports, so there is no reply token to compare;
- the view reads the actor through `actorStore`, which notifies only when the view changes.

**A data fold is a plain reducer.** A data fold turns ordered inputs into a value and has no mode:
the run event fold (`@vibesys/run-state`), a host's listing, recent runs. It is a pure
`(state, action) -> state` on `@vibesys/store`, tested with `checkModel`. A package that owns
control state keeps its folds in their own modules and uses `createStore` nowhere else.

**Resources.** XState drops a late output but cannot stop the work that produces it: a
`fromPromise` body keeps running after its actor stops. So an operation that opens a resource
returns it, and the machine installs it in that operation's `onDone` action, from where leaving the
state disposes it. An operation that holds a resource only while it runs either reads its `signal`
and disposes what it opened when that is aborted, or leaves teardown to the owner of the
connection it ran on, which tears down whatever they left open when the shell closes it.

**Failures.** Every invoke has an `onError` as well as an `onDone`. The shell's operations report
expected failures as output (a snapshot load resolves to `{ok: false, error}`), and
`onError` handles a rejection the same way, so an unexpected throw ends the flight instead of
leaving its region waiting. An action can throw too (a fold or a connection method), which stops
the actor; the shell observes that with an `error` observer and releases what it holds.

**Tests.** A machine is tested three ways, all scaffolded by `pnpm new:package feature <name>`:
pure transitions through `getNextSnapshot` (or `transition`, which also returns the actions);
`checkMachine` from `@vibesys/machine/testing`, which folds seeded random events (plain events,
and the done, error, and fire outcomes of whatever is live) and checks after every step that each
live operation and armed delay belongs to an active state, that no delay is armed twice or
cancelled while not armed, that a final state holds nothing, and the model's own invariants; and,
for stale replies, a live actor whose operations the test settles in a chosen order. Migrating a
reducer to a machine keeps its seeded invariants (ported through the kit) and its shell's
behavioral tests, changed only where an operation's reply now lands a step later (a test then
waits for the published state instead of reading it at once), and deletes the reducer in the same
change.

**Why.** Hand-rolled control state kept producing the same bugs: a reply token compared against the
wrong slot, a timer not cleared on one exit path, an abort controller that outlived its owner.
Each fix added bookkeeping that the next change missed. A machine makes "this work belongs to
this state" structural, and the kit checks it over every seeded interleaving.

**Rejected alternatives.**

- *Effect.ts.* Its fibers and scopes solve cancellation, but the model-based tests that are the
  core of this codebase's testing cannot enumerate or step a fiber: the seeded invariant harness
  would not port. XState keeps the transition pure (`transition` is a function), so it does.
- *No library.* Reducers with reply tokens plus hand-written timer and abort bookkeeping in the
  shell were what we had. Cancellation and timing lived in the shell, outside the tested core,
  and kept regressing.
- *XState for folds too.* A fold has no transitions to model: wrapping it in a one-state machine
  adds a runtime and an indirection and checks nothing more than `checkModel` does.

**Pitfalls.** Inside `setup`, `enqueueActions` does not type-check under
`exactOptionalPropertyTypes`; use `assign` and parameterized actions. Actions in one transition see
each other's `assign`s in order, so an action that must read the previous context runs before the
`assign`. A placeholder actor typed differently from the real one makes `provide` reject it; type
the placeholder with the operation's input and output. `always` guards should read context, not
the event. Every invoke gets an explicit `id` (the kit requires it). In a parallel state, handle an
event at one level only: a region's own transition for an event replaces its parent's, and regions
coordinate through events they `raise`, which every region takes in the next step, with guards that
read the state before it. A `reenter` transition declared on a region exits the whole parallel
state, so re-enter a region's child from the child itself. An actor notifies its observers on
every event, also one that changed nothing, so a shell that publishes changes compares snapshots
by identity. An outcome can go stale between the operation's end and the machine taking it (a
channel the operation opened ends in between): guard `onDone` with a guard the shell provides, and
give it a fallback transition whose action hands the resource back, as `@vibesys/connection`'s
`current` guard and `discard` action do. A `fromPromise` signal aborts when its actor stops before
its outcome was delivered, so an operation disposes what it opened on that abort. The pure
`transition` keeps taking events in a final state, which the live actor does not: a model stops
generating events once the snapshot's `status` is `done`.

**Where XState may appear** is rule 9: the `external-dependencies` table confines the import, and
two GritQL rules (wired in `biome.json`) keep features' `state/` and `effects/` (and
`connection`'s `state/`) free of `setTimeout`, `setInterval`, and `AbortController`, and keep
migrated control-state packages (`run-control`, `connection`, `run-session`) off `createStore`
(except their fold modules, such as `run-session`'s `run.ts`). The features still on the reducer
kernel are migrated one per change. Until then the two effects modules that hand-roll a timer or
an abort controller (`hosts`' path completion, `gateway-connect`'s attempt) are listed as
exclusions in `biome.json`, a list that only shrinks; the remaining migrations and those
exclusions are tracked in #1969.

| Control state | Form | Status |
| --- | --- | --- |
| `run-session`'s session lifecycle (`sessionMachine`, `WebSession`) | machine | migrated (#2071) |
| `feature-run-control`'s commands and banner | machine | migrated (#1950) |
| `@vibesys/connection`'s `HostConnection` | machine | migrated (phase 8) |
| `feature-start-run`, `feature-hosts`, `feature-gateway-connect`, `feature-replay` screen stores | reducer with reply tokens | one feature per change |

## Build model

The workspace stays fast as the package count grows because nothing waits on a build it does not
need, and nothing repeats work whose inputs did not change.

**Source condition.** Every export carries a custom condition that points at its TypeScript source:

```json
"exports": {
  "./state": {
    "@vibesys/source": "./src/state/index.ts",
    "types": "./dist/state/index.d.ts",
    "default": "./dist/state/index.js"
  }
}
```

Vite (`resolve.conditions`), the Bun test runner (`--conditions`), and `tsc`
(`customConditions` in `clients/tools/tsconfig.base.json`) all resolve that condition, so the dev
server, tests, and type checks read dependencies from source and never need a prebuilt `dist/`.
Node and Electron at runtime do not set the condition and load `dist/`. There are no `paths`
aliases and no `pre*` "build dependencies first" scripts.

**Project references.** Each entry is a `composite`, `incremental` TypeScript project; the
package's `tsconfig.json` is a solution file that references its entry projects and its test
project, and each entry project references the entry projects of the packages it imports.
`tsc -b` then type-checks and emits declarations per project, skipping every project whose inputs
are unchanged, and checks a dependent against its dependencies' declarations instead of their
source.

**Task runner.** [Turborepo](https://turborepo.com) runs `build`, `check`, and `test` across the
workspace in dependency order, in parallel, with a local content-hash cache in `.turbo/cache` at
the repository root (shared by the git worktrees of one clone; CI keeps none between runs). A task
whose inputs (the package's tracked files, its dependencies' hashes, and the shared
configuration) match a previous run replays its output instead of running. `test` depends on a
`transit` task rather than on `^build`, so tests start at once while still being invalidated
when a dependency's source changes. Turborepo was chosen over Nx because it needs one
`turbo.json` and the existing `package.json` scripts, with no plugins, generators, or project
files per package; Nx's extra capabilities (code generators, module-boundary lint) are covered
here by the scaffold and the graph checker.

| Command (from `clients/`) | What it runs |
| --- | --- |
| `pnpm check:clients` | `turbo run check`: `tsc -b` in every package |
| `pnpm test:clients` | the script tests, then `turbo run test` |
| `pnpm build:clients` | `turbo run build` |
| `pnpm turbo run test --filter @vibesys/<package>` | one package, building only what it needs |

## Adding a package

```bash
cd clients
pnpm new:package <layer> <name> [--runtime neutral|dom|node]   # pnpm new:package domain runs
pnpm new:package feature <name>                                  # pnpm new:package feature stop-run
pnpm install
```

`tools/scripts/new_package.mjs` writes the `vibesys` block first and derives everything else from it:
the `exports` map with the source condition, one composite `tsconfig.<entry>.json` per entry with
its runtime's `lib` and `types`, the test project and solution `tsconfig.json`, the `build`,
`check`, and `test` scripts, a passing smoke test per entry, and the entries in
`tools/tsconfig.architecture.json` that dependency-cruiser resolves. Knip derives its policy from the
exports, and Biome and Turborepo cover the package by glob. A feature lands in
`features/<name>` as `@vibesys/feature-<name>` with `./state`, `./renderer`, and `./main`; an app
in `apps/<name>` as `@vibesys/app-<name>`; a feature's `./state` also starts from a
control-state machine skeleton, its pure-transition test, and its `checkMachine` model (see
[State](#state)); any other layer (a library) in `libs/<name>` as `@vibesys/<name>` with one `.` entry
(`neutral` unless `--runtime` says otherwise). Its test scaffolds one package of each shape and
requires the graph checker, the workspace declarations, `tsc -b`, and the package's own tests to
pass, and a Node import in a scaffolded renderer entry to fail compilation.

Add a workspace dependency with `pnpm --filter <package> add @vibesys/<dependency>@workspace:*` and
a matching project reference in the entry project that imports it; `pnpm check:ts-architecture`
names a missing or extra one.

## Migration status

| Package | Layer | Status | Item that creates or moves it |
| --- | --- | --- | --- |
| `@vibesys/contracts` | foundation | exists (pilot: generated run-protocol and host-helper types, the one schema walker, error taxonomy, `./host-helper` validation, `./testing` fixtures) | foundation item (#1869) |
| `@vibesys/ui` | foundation | exists: `Button`, `Combobox`, `Dialog`, `StatusChip` (`.`, `dom`) and the design tokens (`./tokens.css`); the desktop and web features render them | domain packages (phase 5 of #1878) |
| `@vibesys/store` | foundation | exists: `Step`, `Reducer`, `only`, `createStore` (`.`) and the fold model-test kit (`./testing`: `checkModel`, `Seeded`, `SEEDS`); domain-free | domain split (#1949) |
| `@vibesys/run-state` | core | exists: the run event fold (`.`), with `./rounds`, `./performance`, and `./transcript` projections; depends on `contracts` only | domain split (#1949) |
| `@vibesys/core-state` | core | deleted: split into `store` and `run-state` | domain split (#1949) |
| `@vibesys/transport` | core | exists: `.` and `./testing` (the shared `FakeClock`) are `neutral`, `./websocket` is `dom` | backend-client split (#1910) |
| `@vibesys/transport-node` | domain | exists: the Unix-socket `ServerClient`; desktop's ssh and local-host links join it with the connection manager | backend-client split (#1910) |
| `@vibesys/hosts` | core | exists: host identity and keys, labels, the checkout and project path rules (`HostValidationError`), a host's listed runs with their phase text (`instancePhaseText`), and what the window shows about a connection; depends on `contracts` | domain split (#1949) |
| `@vibesys/runs` | domain | exists: run, task, and instance ids, a recent run's status from its host's listing, the exact-resume policy, the stop flow; depends on `contracts`, `hosts` | domain split (#1949) |
| `@vibesys/machine` | core | exists: `actorStore`, `clockFrom`, `unprovided` (`.`) and `checkMachine` (`./testing`); domain-free | state architecture (#1950) |
| `@vibesys/run-session` | domain | exists: the run fold's wiring (`runStep`), the session lifecycle (`sessionMachine`, three parallel regions: start-up, recovery, and the fenced replacement snapshot), its ports, the run connection over a control transport (`connectRun`), and `WebSession`, with `./testing` (an in-memory control transport and page lifecycle, and `sessionWith`) | domain split (#1949) |
| `@vibesys/entities` | domain | deleted: split into `hosts` and `runs`; task arguments (`shellWords`) moved into `@vibesys/feature-start-run` | domain split (#1949) |
| `@vibesys/ipc` | service | exists: the run page's bridge (port messages, their validation, the preload contract web's desktop shell reads) and the welcome view's channels, events, and payloads, with each feature's requests (channels, bridge slice, and the parser the main process runs) in `welcome/<feature>.ts`, and `./testing` (random window states for store models); depends on `hosts`, `runs`, `transport` | domain packages (phase 5 of #1878); per-feature parsers with phase 8 |
| `@vibesys/connection` | service | exists (`.`, `node`): the connection manager (`ConnectionManager`, `HostConnection`: an XState machine in `state/` and a `shell/` owning the links, channels, and the machine's clock), role-aware ssh admission and spillover-master leases, the ssh and local links, the bootstrap, bounded pool diagnostics, and `connectionStatus`, with `./testing` (`FakeHelper`, `FakeLink`, and the `HostConnection` contract suite); depends on `contracts`, `transport`, `hosts` | desktop main process (phase 8 of #1878) |
| `@vibesys/settings` | service | exists (`.`, `node`): the settings store (`hosts.json`, `recent.json`; one writer, atomic replace), with `./testing` (`FakeSettingsFilesystem`); depends on `hosts`, `runs` | desktop main process (phase 8 of #1878) |
| `@vibesys/feature-run-view`, `@vibesys/feature-gateway-connect`, `@vibesys/feature-replay` | feature | exist: the run page's transcript, rounds, status, and banners (`./state`, `./renderer`; the live session is `@vibesys/run-session`); the browser's gateway connect form (`./renderer`); the recorded-run load (`./state`, `./renderer`) | feature packages (phase 6 of #1878) |
| `@vibesys/feature-hosts`, `@vibesys/feature-start-run`, `@vibesys/feature-run-control`, `@vibesys/feature-recent-runs` | feature | exist, renderer side (`./state`, `./renderer`): the host panel, checkout form, sign-in, "Connect to host", and path completion; the "Start a run" sheet; the title strip, stop, force stop, resume, and the banner; Recent. Each feature's effects call only the slice of `@vibesys/ipc`'s welcome bridge its `./state` declares. `feature-hosts` and `feature-run-control` also publish their main-process controllers (`./main`: `Hosts`; `RunControl` with its relay and stop flow) | desktop renderer (phase 7 of #1878); `./main` with the main-process split (phase 8) |
| `apps/web` | app | exists: composition only, on the per-entry layout (one `dom` entry, no exports): the browser entry and boot decision, the desktop shell's run page (`desktop-main.tsx` and the bridge transport), the one backend adapter, storage and lifecycle. Its run connection (`connectRun`) is `@vibesys/run-session`'s and the run page's styles are the features' own (`./styles.css`). The desktop run page still moves to `apps/desktop-renderer`: what remains is the page lifecycle (`platform/lifecycle.ts`) and the live run page's composition (`createLiveApp`), which both apps would use, the bridge transport (`desktop-shell.ts`), and a second Vite page in that app's build, which `apps/desktop-main`'s `copy-ui.mjs` then copies | feature packages (phase 6 of #1878) |
| `apps/desktop-renderer` | app | exists: composition only (one `dom` entry, no exports): the welcome view's entry, its stores wired to the welcome preload's bridge, and the view composed from the desktop features | desktop renderer (phase 7 of #1878) |
| `apps/desktop-main` | app | exists: composition only (Electron main, `.`, `node`): the lifecycle, page confinement, the window, launch arguments, and the welcome requests routed to the features' `./main` controllers; and the two sandboxed preloads (`./preload-run`, `./preload-welcome`, `preload`). Its build copies the two page bundles into `dist/ui` | desktop main process (phase 8 of #1878) |
| `@vibesys/backend-client` | core | deleted: split into `transport` and `transport-node` | backend-client split (#1910) |
| `@vibesys/desktop` | app | deleted: emptied into `connection`, `settings`, the features' `./main`, and `apps/desktop-main` | desktop main process (phase 8 of #1878) |
| `@vibesys/web` | app | moved: features into `features/*`, the composition into `apps/web` | feature packages (phase 6 of #1878) |
| `@vibesys/tui` | app | deprecated; kept building, not migrated | none |

Roadmap [#1878](https://github.com/uw-syfi/vibesys/issues/1878) tracks this migration; each item gets
a sub-issue there when it starts.
