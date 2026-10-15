# Desktop app redesign

Status: proposal, awaiting approval. No product code changes until it is approved.

This is the implementation spec for the redesigned VibeSys desktop app (Electron, `clients/`). The
screens and copy were agreed with the user in a clickable prototype (the "Flow" page of the
prototype canvas: New run, Run, Machines, Settings, the shared Sidebar, and "Today" for
comparison). The prototype is the source of truth for layout and copy; this document says how to
build it so that every screen comes out of one design system, and how checks keep it that way.

It builds on, and does not restate:

- [`clients-architecture.md`](../contributing/clients-architecture.md): the package graph, layers,
  entry runtimes, the package checker, and the State rule (control state is an XState machine,
  data folds are plain reducers).
- [`desktop-connection.md`](../contributing/desktop-connection.md): the host helper, its methods and
  feature gates, run identity, and screen stores.
- [`wire-protocol.md`](../contributing/wire-protocol.md): the run protocol.
- [`campaign-workspace-ui.md`](campaign-workspace-ui.md): the campaign dashboard, whose live
  projection lacks the objective, benchmark versions and trajectories.

The web client is being deprecated. Everything here targets the desktop app only.

## Contents

1. [What changes for the user](#what-changes-for-the-user)
2. [Design system](#design-system)
3. [Enforcement](#enforcement)
4. [Backend data](#backend-data)
5. [Packages and screen stores](#packages-and-screen-stores)
6. [PR order](#pr-order)
7. [Open questions](#open-questions)

## What changes for the user

| Today | After |
| --- | --- |
| A welcome view with a host panel, a Start sheet and a Recent panel; the run opens in the same window as a separate web page. | One window with a left sidebar: New run, Live runs, Recent runs (last 20, then "Show all"), and Machines and Settings at the bottom. No top tabs. |
| Starting a run is a sheet with free-text arguments. | New run is one screen: Machine, Project and task, Agent, Budget and results, and a "Ready to start" column whose failed checks carry their fix. |
| Hosts are added by connection details; the checkout is implicit. | Machines: an SSH host plus an existing VibeSys checkout path, checked and suggested by path completion. Statuses: Ready, Checkout not found, Sign in. |
| The run page shows rounds and a startup log; the campaign dashboard is web-only. | The Run screen: a header with Pause/Resume, Stop (confirms what is lost), Force stop (offered after a Stop hangs) and a "⋯" menu; the startup timeline; a time scrubber; tabs Overview, Map, Work and Logs. |
| Light or dark follows the OS; the run view forces dark. | Settings → Appearance: Match system, Always light or Always dark, with three light themes (Paper, Mist, Sand) and three dark themes (Ink, Midnight, Terminal). Notifications when a run finishes, fails or pauses. |

The word is always "run", never "campaign", in every label, title and message.

Not in v1: Ask/Steer, a candidate screen, spending limits, and run comparison. Run identity
(`(host, run_id)`) and the run overview contract below are kept stable so comparison can be added
without a contract change.

## Design system

The design system is `@vibesys/ui`, extended in place. It stays a `foundation` package (rank 0,
`dom` runtime, domain-free), so the existing `layer-direction` and `domain-free` rules keep holding
it: no product word (run, host, agent, checkout, ...) appears in its source. A new package was
considered and rejected: two UI libraries would invite the question of which one a feature should
use, and `@vibesys/ui` already has the right place in the graph and the right consumers.

`@vibesys/ui` publishes:

| Entry | Runtime | Contents |
| --- | --- | --- |
| `.` | `dom` | Components, layout primitives, the `ThemeName` and token types, `applyTheme`. |
| `./tokens` | `neutral` | The token tables as typed data (`THEMES`, `SPACE`, `TYPE`, `RADIUS`, ...), and `contrastRatio`. Read by tests, the generator and the settings feature's theme list. |
| `./tokens.css` | `dom` asset | Generated from `./tokens`; never edited by hand. |
| `./fonts.css` | `dom` asset | `@font-face` for the bundled fonts. |

### Look

The prototype's original look is final: high contrast, hairline (1 px) borders, monospace accents
for labels, numbers and identifiers, a sans body, flat surfaces with no shadows except overlays,
and the large ASCII VIBESYS wordmark as the New run hero. It is Vercel-like in spirit without
copying Vercel: our own fonts, our own colors, and our own component shapes.

Fonts are Schibsted Grotesk (sans) and JetBrains Mono (mono), both under the SIL Open Font
License, bundled as `woff2` files inside `@vibesys/ui`. The app's session is locked down
(`lockDownSession`), and visual tests must not depend on the network, so nothing is loaded from
Google Fonts.

### Tokens

Every token is a typed constant in `./tokens`, and `tokens.css` is generated from it. One source
of truth per fact: the TypeScript tables are the source; the CSS, the theme list in Settings, and
the contrast test all read them.

**Color roles.** A theme is a `Record<ColorRole, Hex>`, total over `ColorRole`:

| Role | Use |
| --- | --- |
| `bg` | Page background. |
| `panel` | Cards, fields, menus. |
| `panel-2` | Sidebar, hover, table headers, read-only values. |
| `line` | Hairline borders and dividers. |
| `text` | Body text; the primary button's fill. |
| `muted` | Secondary text, section labels, idle dots. |
| `accent` | Live state, focus ring, selection, links, chart series. |
| `ok`, `warn`, `err` | Status: success, attention, failure. |
| `log-bg`, `log-text` | The log pane, dark in every theme. |
| `grid` | Chart grid lines. |

Derived values (selection tint, status chip fills and borders, banner fills) are computed in
`tokens.css` with `color-mix` from these roles, so a theme defines only the table above. The
primary button inverts `text` and `bg`.

**Themes.** The six themes, with the prototype's values except four status and accent colors darkened slightly (Paper `ok` from `#17803d`, Mist `ok` from `#15803d`, Sand `accent` from `#0f766e` and `ok` from `#3f7d20`): the prototype's values measure 4.0 to 4.4:1 as text on `panel-2` or `bg`, below the 4.5:1 floor the contrast test (S10) enforces. The table passes S10 as written:

| Theme | Mode | bg | panel | panel-2 | line | text | muted | accent | ok | warn | err | log-bg | log-text |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Paper | light | `#f7f6f3` | `#ffffff` | `#f0eee9` | `#e3e0d8` | `#191817` | `#64615b` | `#2f5bea` | `#167a3a` | `#925500` | `#b42318` | `#15161a` | `#d9dde5` |
| Mist | light | `#f3f5f8` | `#ffffff` | `#ebeff4` | `#dbe1e8` | `#0f172a` | `#566173` | `#6d28d9` | `#147a3a` | `#8f5300` | `#b91c1c` | `#111522` | `#d6dcea` |
| Sand | light | `#f2eee5` | `#fbf8f2` | `#ebe5d8` | `#dcd3c1` | `#29231a` | `#675d4b` | `#0e726a` | `#39721d` | `#8a5000` | `#a8321c` | `#1c1913` | `#e6dfcf` |
| Ink | dark | `#0a0a0a` | `#111111` | `#191919` | `#272727` | `#ededed` | `#a0a0a0` | `#4f8cff` | `#3fb950` | `#d6a03a` | `#f2665f` | `#050505` | `#d4d4d4` |
| Midnight | dark | `#0a0f1e` | `#10172c` | `#161f3a` | `#243055` | `#e6eaf5` | `#9ba7c5` | `#9787ff` | `#4cc38a` | `#e0aa4f` | `#ff7a85` | `#060a16` | `#d3daf0` |
| Terminal | dark | `#050806` | `#0a110c` | `#0f1a12` | `#1c2c20` | `#d6f5de` | `#86a891` | `#3ddc84` | `#3ddc84` | `#e3c35a` | `#ff6b6b` | `#020403` | `#bdf0cb` |

`grid` is `text` at 6 to 7 % alpha in each theme. `ThemeName` is the closed union of these six;
`ThemeMode` is `light | dark`; each theme declares its mode, and Settings lists the light themes
under "Light theme" and the dark ones under "Dark theme" from that field. Defaults: Paper (light)
and Ink (dark). Terminal uses one green for `accent` and `ok`; its live and success states are
told apart by the dot's ring and the chip's label, never by color alone (see
[Status dot](#components)).

**Type scale.** Two families (`sans`, `mono`) and a closed set of text styles:

| Style | Family | Size / line | Weight | Notes |
| --- | --- | --- | --- | --- |
| `display` | mono | wordmark only | 400 | The ASCII wordmark; scales down at narrow widths. |
| `title` | sans | 24 / 30 | 600 | Screen title; letter-spacing −0.01em. |
| `heading` | sans | 18 / 24 | 600 | Card and detail titles. |
| `body` | sans | 14 / 20 | 400 | Default. |
| `body-strong` | sans | 14 / 20 | 500 | Row titles, labels. |
| `small` | sans | 13 / 18 | 400 | Help text, details. |
| `caption` | sans | 12 / 16 | 500 | Chips. |
| `code` | mono | 12.5 / 18 | 400 | Paths, ids, models, log text. |
| `section` | mono | 11.5 / 16 | 500 | Uppercase, letter-spacing 0.08em, `muted`. "01 · Machine". |
| `metric` | mono | 22 / 28 | 600 | Key numbers. |

**Spacing.** A 4 px scale, `space-0` to `space-8`: 0, 2, 4, 8, 12, 16, 24, 32, 48. The
prototype's 10, 14 and 18 px gaps round to 8, 12 and 16 (or 24); this is the only intentional
departure from the prototype's pixels.

**Sizes.** Control heights: `control-sm` 32 (segments, compact filters), `control` 40 (buttons,
tabs, fields), `row` 44 (tree rows, list rows; the touch-target minimum). Sidebar width 264. Page
content max widths: `content` 1060, `content-wide` 1100, `content-narrow` 900.

**Radii.** `radius-sm` 4 (bars), `radius` 6 (controls), `radius-lg` 8 (cards, banners, log pane),
`radius-full` (chips, dots).

**Elevation.** Three levels. `flat` (hairline border, no shadow) is everything in the page.
`raised` (hairline plus a small shadow) is menus and popovers. `overlay` is the modal dialog and
its backdrop. Nothing in the page flow casts a shadow.

**Motion.** Durations `instant` 0, `fast` 120 ms, `base` 200 ms; one easing curve. Motion is
limited to hover and focus transitions, disclosure (expand and collapse) and the live dot's ring.
Under `prefers-reduced-motion: reduce` every duration is 0, and the visual tests run in that mode.

**Focus.** A 2 px `accent` outline with a 1 px offset on every interactive component, drawn by
`@vibesys/ui` only.

### Theme application

`applyTheme(name)` sets `data-theme` on the document element; `tokens.css` has one
`:root[data-theme="<name>"]` block per theme. The renderer resolves the theme from the Appearance
setting (mode and the two theme choices) and the OS appearance (`prefers-color-scheme`, observed
as an external source in the settings feature, not in `@vibesys/ui`). Main sets
`nativeTheme.themeSource` from the same setting, so window chrome matches; the current rule that
forces dark while a run is shown is removed. Before React mounts, the preload reads the persisted
theme so the first paint is in the right theme (no flash).

### Components

Every component is a typed React component in `@vibesys/ui` that takes data and callbacks only.
No component accepts `className` or `style`; variants are typed props whose values are token
names. Status is always conveyed by a label as well as color.

**Layout and text primitives** (so features need no CSS at all):

| Component | Purpose and props |
| --- | --- |
| `Page` | Scrolling main area with a content max width (`width: 'content' \| 'wide' \| 'narrow'`) and the page gutter. |
| `Stack`, `Row` | Vertical and horizontal flex with `gap: SpaceToken`, `align`, `justify`, `wrap`. |
| `Split` | Main column plus aside that wraps under it on narrow windows (the New run checks column, the Map detail, the Machines detail). |
| `Grid` | Auto-fit grid with `min: 'field' \| 'tile' \| 'swatch'`. |
| `Text` | One of the type styles above, with `tone: 'default' \| 'muted' \| 'ok' \| 'warn' \| 'err'`. |
| `Wordmark` | Renders a preformatted ASCII art string in the `display` style, scaled to fit; the art itself stays in the app shell, since `@vibesys/ui` names no product. |
| `Icon` | A closed set of stroke icons (plus, chevron, more, check, cross, clock, monitor, gear, ...). |

**Controls and content** (the inventory the brief requires, plus what the screens need):

| Component | Variants and behavior | Used by |
| --- | --- | --- |
| `Button` | `primary` (inverted), `secondary` (hairline), `quiet`, `danger`; `size: 'sm' \| 'md'`; `full`; `disabled` with a reason shown as help text. Renders `<button>`, or `<a>` when given `href`. | Everywhere |
| `SegmentedControl` | Radio group with roving focus; `options: {value, label}[]`. | Project source, Appearance mode, Map filter |
| `Select` | Native `<select>` styled by tokens; options may be disabled with a reason; `mono` option text. | Machine, Task, Harness, Model, Rounds, Time limit, Results |
| `Field` | Label, control slot, help text, error text; wraps `TextInput` and `PathInput` (a combobox over completions, from today's `Combobox`). | Folder, checkout path, extra arguments |
| `Checkbox`, `Switch` | Labeled boolean. | Notifications |
| `Chip` | `tone: 'neutral' \| 'live' \| 'ok' \| 'warn' \| 'err'`, optional leading dot. | Statuses, "builds on" |
| `StatusDot` | Same tones; `live` adds a ring so it differs from `ok` without color; requires `label` (used as its accessible name). | Sidebar, rows, timeline |
| `Card` | Flat panel with optional `section` title. | Every section |
| `Banner` | `tone: 'neutral' \| 'ok' \| 'warn' \| 'err'`; title, body, actions slot. | Stop confirm, stopping, quota, run ended, nested repository |
| `Menu` | Button-anchored menu (`raised`), keyboard navigable, closes on Esc and outside click. | Run "⋯", row menus |
| `Dialog` | Today's native `<dialog>` sheet, restyled. | Confirmations that block |
| `Table` | Columns typed by the caller, sticky header, horizontal scroll in its own box, row selection. | Work tab, role table |
| `Tabs` | `tablist` with arrow-key navigation; the panel is a render slot. | Run tabs |
| `TreeRow` | Indented row (`depth`), disclosure for groups, state dot, title, subtitle, trailing chips, selected state; the tree's keyboard model (up, down, left, right). | Map |
| `KeyValue` | Label column plus value, `mono` values. | Machine facts, Map detail |
| `CheckList` | Rows with `ok \| warn \| fail \| pending` icon, label, detail and an optional fix action. | Ready to start |
| `Timeline` | Stages with dot, label, elapsed time, a "quiet for" chip and an expandable body slot. | Startup |
| `LogPane` | Monospace, `log-bg` in every theme, follows the tail while at the bottom, stops following when the user scrolls up, offers "Jump to latest"; stderr spans in `err`. Renders text only, never HTML. | Startup log, Logs tab |
| `Scrubber` | Range over a sequence domain with labeled ticks, a playhead, "Live" or "Replay" state and a "Back to live" action; keyboard steps. | Run |
| `Stat` | `section` label, `metric` value, `small` sub-label. | Overview key numbers |
| `Chart` | A time or index series chart in SVG: points (filled or hollow), a step line, a dashed reference line, a playhead, axis labels; colors from `accent`, `muted` and `grid` only. | Performance |
| `SidebarItem` | Nav link with icon, label, count or status dot and a detail line; `active`; an optional row menu. | Sidebar |
| `SidebarSection` | `section` heading plus items plus an optional "Show all" link. | Sidebar |
| `EmptyState` | Title, body and action. | Empty lists |

## Enforcement

The rule: redesign screens are built only from `@vibesys/ui`'s public components and tokens. It is
rolled out without touching existing code: new screens go in new, strict packages; old packages are
frozen until they are deleted; and the last PR makes strict the default. There is no baseline file.

### Strict packages

A package opts in with a marker in its manifest, which stays the one source of truth for its place
in the graph:

```json
"vibesys": {"layer": "feature", "design": "strict", "entries": {"./state": "neutral", "./renderer": "dom"}}
```

`design` is a closed set (`strict` or absent); `declarations` already rejects unknown keys and
values by name, and gains this one. The redesign's packages (see
[Packages](#packages-and-screen-stores)) are all strict from their first commit.

### Rules and what enforces them

| # | Rule | Enforced by | Rung |
| --- | --- | --- | --- |
| S1 | Spacing, sizes, radii, type and colors in strict code are token names, never raw values | Types: layout and text components take `SpaceToken`, `TextStyle`, `Tone`, ... unions, and no component accepts `className` or `style` | Type |
| S2 | A strict package ships no stylesheet | `check_package_graph.mjs` rule `strict-no-stylesheets`: a strict package exports no `*.css` entry and its source holds no `.css`, `.scss` or CSS-in-JS file | Graph |
| S3 | Strict code renders no raw interactive or styled elements | Grit rule `strict-no-raw-elements.grit`: in strict packages, JSX intrinsic `button`, `a`, `input`, `select`, `textarea`, `label`, `dialog`, `details`, `summary`, `form`, `table`, `svg` and `pre` are errors, and so are the `style`, `className` and `dangerouslySetInnerHTML` props on any element. Plain `div` and `span` remain for grouping text inside slots | Lint |
| S4 | Strict code contains no raw colors or lengths | Grit rule `strict-no-raw-values.grit`: string and template literals matching a hex color, `rgb(`, `rgba(`, `hsl(`, `oklch(`, `color-mix(`, or a CSS length (`<number>px`, `rem`, `em`, `vh`, `vw`) are errors | Lint |
| S5 | The strict lint rules cover exactly the strict packages | `check_package_graph.mjs` rule `strict-lint-coverage`: the `includes` of `biome.json`'s strict override equal the set of packages marked strict, so a new strict package cannot escape S3 and S4, and a package cannot be listed without the marker | Graph |
| S6 | Strict packages import UI only from `@vibesys/ui`'s public entry, and import no old feature package | `check_package_graph.mjs` rule `strict-imports`: a strict package's workspace imports are `@vibesys/ui` (entries `.` and `./tokens`) and non-UI libraries (`contracts`, `store`, `machine`, `run-state`, `run-session`, `transport`, `hosts`, `runs`, `logs`, `ipc`, `connection` and `settings` from `./main`); its third-party dependencies are `react`, `react-dom` and `xstate` only (no Radix, Tailwind, class-variance-authority, `cn` or icon packages). Deep imports are already refused by `imports-use-exports` and dependency-cruiser's `workspace-packages-use-public-exports`; feature-to-feature imports by `layer-direction` | Graph |
| S7 | Apps compose strict packages' screens without adding UI of their own | `strict-imports` applies to the new shell's renderer entry as well (the app's renderer composition is marked strict; only `main.tsx` wiring and the generated CSS imports live there) | Graph |
| S8 | `@vibesys/ui` itself uses tokens: raw colors appear only in the token table | `strict-no-raw-values.grit` also runs on `libs/ui/src`, excluding `tokens.ts` (the table) and the generated `tokens.css`; a Biome CSS lint rule or Grit CSS pattern bans hex and length literals in `libs/ui/src/*.css` outside custom-property definitions that read the table | Lint |
| S9 | `tokens.css` is generated from the token table | A `tui_quality` step regenerates it and fails on a diff, as the contract bindings are checked today | CI script |
| S10 | Every theme meets contrast floors | Deterministic test `test_every_theme_meets_contrast_floors` in `@vibesys/ui`: for each theme, each pair in `CONTRAST_PAIRS` (text on bg, panel and panel-2; muted on bg, panel and panel-2; each of ok, warn, err and accent as text on panel; log-text on log-bg; bg on text for the primary button) is at least 4.5:1, and focus ring, status dots and chart marks against their surfaces at least 3:1. Property: the floors hold for every theme in `THEMES`, so a seventh theme is checked with no test change. Uses `contrastRatio` from `./tokens` (moved down from the TUI's copy) | Test |
| S11 | Every component renders correctly in every theme | Visual test: a gallery page (`apps/ui-gallery`, dev only) renders every public component in each of its states; Playwright `toHaveScreenshot` per theme per component, with bundled fonts, reduced motion, a fixed viewport and no clock (time-based values are passed as props). Runs in the real tier with the Playwright jobs. Meta-test `test_gallery_covers_every_public_component`: the gallery's registry keys equal the names exported from `@vibesys/ui`'s `.` entry, so a new component without a gallery entry fails | Test |
| S12 | Components are accessible | The gallery spec runs axe on each page per theme and fails on any violation; the `StatusDot` `label` prop and `Button`'s required text or `aria-label` are required by type | Test, type |
| F1 | Old feature packages get fixes only, no new UI | CI script `check_frozen_ui.mjs`, run in `tui_quality` against the merge base: in a package listed in `FROZEN_UI_PACKAGES` (in the checker's tables), the PR adds no file under `src/renderer/`, adds no export to a `./renderer` entry, and raises no file's count of JSX intrinsic interactive elements or raw color literals. The script takes the base and head file trees as arguments; its tests drive it with synthetic trees, no Git. The list only shrinks: a package leaves it when it is deleted | CI script |
| F2 | Strict becomes the default | The final PR flips `strict-*` rules to apply to every package unless it is in `FROZEN_UI_PACKAGES` (empty by then), removes the `design` marker and `strict-lint-coverage`, and points the strict Biome override at `features/**` and `apps/**` | Graph |

Every rule's failure message names the file and line (or the manifest key), the rule id, and the
fix ("use `<Button>` from `@vibesys/ui`", "use a `SpaceToken` such as `'4'`"). Each check's tests
build one violating and one compliant fixture per rule; the package checker's meta-test (a rule
with no failing fixture fails) already covers the new graph rules.

Frozen at the first PR: `features/campaign`, `features/run-view`, `features/start-run`,
`features/hosts`, `features/recent-runs`, `features/run-control` (renderer), `features/conversation`,
`features/gateway-connect`, `features/replay`, `features/web-theme`, `apps/web`, and the
desktop renderer's welcome view.

Unenforced: whether a screen matches the prototype's layout and copy needs a reader; the visual
tests hold the components, not the screens. Each screen PR attaches screenshots in every theme.

### What happens to existing UI

- **`features/web-theme`'s palette.** Its eight palettes mirror the TUI's theme names and are chosen
  by the host. The new token table in `@vibesys/ui` replaces it as the one theme source for the
  desktop app; the desktop app no longer takes a theme from the host (the user picks in Settings).
  The web palettes are not ported: their names belong to the TUI, which is deprecated. `web-theme`
  is frozen, stays only for `apps/web`, and is deleted with it.
- **The campaign feature's Radix and Tailwind components** are not moved into `@vibesys/ui` and are
  not imported. The pieces worth keeping (the performance chart, the activity timeline, the work
  history table and its filters) are rebuilt in `run-page` on `@vibesys/ui`. Pure data logic worth
  keeping (trajectory and analytics computations in `campaign-analytics.ts` and the frame fold's
  rules) moves down into `@vibesys/run-state` (its `./performance` entry) in the PR that first
  needs it, with its tests. `features/campaign` keeps its Radix and Tailwind dependencies until it
  is deleted.
- **Screens being deleted** (the welcome view, the Start sheet, the Inspect sheet, the campaign
  view, the run page's current layout) need no migration; they are removed at cutover.

## Backend data

Five additions make the screens honest; the first changes the flow framework itself. Each is a typed, versioned contract with one authoritative
Pydantic definition and generated TypeScript (`check_contract_sot`). Helper additions ship behind
new `HelperFeature`s per [Handshake and versioning](../contributing/desktop-connection.md#handshake-and-versioning);
run protocol additions follow the run protocol's capability probe until it has a `hello` of its
own. No contract exposes `vs_core`'s `FlowState` or the dynamic search's private records.

### B1. Run trace: spans recorded by the runtime

**What exists.** `vs_core`'s `FlowState` is a replay journal plus live flows identified by path; it
has no kinds, states, timestamps or edges. The task model the Map shows (planning calls,
workstreams, agent turns, gates, the baseline) lives in the dynamic search's state
(`src/vibesys/orchestration/dynamic/search/`: `WorkPlan`, `Running`, `FinishedWork`, `Failure`,
`RoundRecord`, `PlannerGate`), which keeps no timestamps and is not on the wire. The run protocol's
`RunEvent` carries no workstream or parent id.

**Design.** The runtime owns a run trace modeled on tracing spans. Lifecycle facts come from the
runtime, which already journals every transition; meaning comes from the search, which labels its
own spans through `ctx` with labels it declares at registration. This is a framework capability in
`vs_core.flows` and `ctx`, added and reviewed under the rules of
[flows.md](../contributing/flows.md) ("A capability a flow lacks is added here, to the framework,
and reviewed; never hand-rolled in a search"). No search builds its own projection, and nothing
serializes `FlowState`.

**Runtime fields.** The runtime records a span for each flow task in a group and for each awaited
operation (agent turn, evaluation stage, workspace, owner operation), at the place where the
transition is journaled:

```text
Span
  id: SpanId          deterministic: from the flow path and task key, or the operation's decision id;
                      stable across replay and restart
  parent: SpanId | null
  type: flow | operation
  name: str           the flow's name, or the operation's registered name
  status: pending | running | waiting | done | failed | stopped        lifecycle only
  waiting_on: [{kind: operation | slot | time | first | stop, ref, since}]
  attempts: [{n, of, started_at, ended_at, outcome: done | again | gave_up, reason}]
                      from ctx.retry's Tries and GaveUp
  started_at, ended_at: run-clock time when the step is journaled, recorded in the journal,
                      so replay yields the same times
  failure: {kind: outcome | interruption | defect, message} | null      flows.md's three kinds
  stop: {requested_at, landed} | null
  label: Label | null  set by the search, below
```

**Search labels.** A search declares its span kinds at `@search` registration, as it declares
roles and operations:

```python
@search("dynamic", config=DynamicConfig, roles=ROLES, operations=OPERATIONS,
        spans=(
            SpanKind("planning_call", attrs=PlanningAttrs, verdicts=()),
            SpanKind("workstream", attrs=WorkstreamAttrs,
                     verdicts=("accepted", "rejected"), links=("builds_on",)),
            SpanKind("agent_turn", attrs=AgentTurnAttrs, verdicts=()),
            SpanKind("gate", attrs=GateAttrs, verdicts=("passed", "failed_check", "slower")),
        ))
```

and labels spans with one new `ctx` call:

| Operation | What it does |
|---|---|
| `ctx.label(kind, title=..., links=..., attrs=..., verdict=..., reason=...)` | Labels the current task's span. A later call replaces the fields it names. Journaled like every other `ctx` effect. |
| `label=Label(...)` on an aspect call (`session.turn(..., label=...)`, `ctx.evaluation.measure(..., label=...)`) | Labels the operation's span when it is issued. |

- `kind` is one of the search's declared kinds; each kind has its own attribute schema (a pydantic
  model with `extra="forbid"`): a workstream's `hypothesis_id` and `parent_revision`; an agent
  turn's `role`, `provider`, `model` and `effort`; a gate's `stage`, `metric_value` and
  `vs_baseline`.
- `title` is display text.
- `links: [{rel, to: SpanId}]` with relation names the kind declares (`builds_on`). Links are
  first-class, so an agent graph with fan-in needs no protocol change later.
- `verdict` comes from the kind's declared value set, with a `reason`, and is kept separate from
  `status`: a workstream can be `done` and `rejected`.
- An undeclared kind, attribute, relation or verdict is rejected when the label is applied, with
  an error naming the key (rule 7: parse at boundaries); a link to a span that does not exist is
  rejected the same way.

**On the wire.** Three generic run events, `span_started`, `span_updated` and `span_ended`, carry
the whole span (no field patches) with the event's `sequence` and `timestamp`; the snapshot carries
the current spans, and each declared kind's attribute schema is exported in the `ProtocolDocument`
so the TypeScript types are generated. The UI renders any search's spans: kinds it knows
(planning call, workstream, agent turn, gate) get their own presentation, and unknown kinds render
plainly from name, title, status, attrs and links.

**What else hangs off span ids.**

- Log-store owners are span ids, so a Map node opens exactly its logs (`LogOwner` gains the span
  id; `logs.list` can be filtered by it).
- Usage records (`turn_usage_recorded`) attach to agent-turn spans.
- The Overview's "Current work", the Work history, the performance chart (gate spans with a
  `metric_value`) and the key numbers are queries over the same spans; there is no second record of
  the run's work.
- The scrubber folds span events up to a sequence, live or ended (see B5).

**Properties to test** (in `vs_core` with `FlowRun` stories and in the dynamic search with seeded
schedules; `begin` already runs every story live and with a restart after every step):

- Replaying a journal yields identical spans, ids and times.
- A span's parent exists before the span is started.
- Every link names an existing span.
- Folding all span events equals the snapshot's spans.
- An ended run has no `running` or `waiting` span.
- An undeclared kind, attribute, relation or verdict is rejected naming the key.

### B2. Run overview: objective, benchmark versions and trajectories

**What exists.** The objective reaches the wire only as its first line (`RunConfiguredData`) and a
280-character paragraph (`PerformanceContext`); per-round metrics answer `query.performance`;
per-candidate readings (`MetricRow`, `AcceptedReading`) are internal. The campaign projection's
`objective`, `benchmarkVersions` and `trajectories` are therefore empty for live runs.

**Design.** A `RunOverview` contract (`overview_version: 1`) in the snapshot:

- `objective`: the task's `OBJECTIVE.md` text (Markdown, rendered as text with headings and lists
  only), the metric's name, unit and direction, and the correctness requirement in one line.
- `benchmarks`: the evaluator's name and version and each gate's tool and version, as measured
  (from the evaluator build and the gate records), so a later comparison can tell runs on
  different benchmark versions apart.
- Trajectories are not a separate contract: each measurement is a gate span with `metric_value`
  and `vs_baseline` attributes and a verdict (B1), so the chart, the Work tab and the key numbers
  (best vs baseline, round, workstreams live and waiting, elapsed) are queries over spans.

`query.performance` and `PerformanceContext` stay for the frozen web run page until it is deleted.

### B3. Machines and the "Ready to start" checks

**What exists.** `hello` returns `checkout: {root, commit, dirty} | null`; `paths.complete` marks
entries that are VibeSys checkouts, projects and Git repositories; `tasks.list` and
`harnesses.list` exist. Nothing reports hardware, Docker, the agent image or the evaluator; there
is no preflight; `run.start` takes free-text `args` for rounds and limits.

**Design.** New helper methods, each behind its own feature, loaded lazily so `hello` stays within
the import ceiling (as `harnesses.list` is):

| Method | Feature | Result |
| --- | --- | --- |
| `machine.describe` | `machine_describe` | `checkout {root, version, commit, dirty}`, `hardware {cpu_cores, gpus: [{vendor, model}], accelerator: cpu \| cuda \| rocm \| metal}`, `docker {reachable, version} \| {reachable: false, reason}`, `harness_clis: [provider]` installed, `runs {live, ended_today}`. |
| `checkout.inspect` | `checkout_inspect` | For a path: `found {version, commit, dirty, compatible}` or `not_found` or `too_old {version, min}`. Path completion stays `paths.complete`. |
| `examples.list` | `examples` | The bundled examples and their tasks. |
| `project.create_from_example` | `examples` | Copies an example into its own new Git repository at a destination; returns the project path. |
| `project.copy_out` | `examples` | Copies a folder that sits inside another repository into its own new repository; returns the new path. Refuses a destination that exists. |
| `run.preflight` | `preflight` | For a project, task, harness and options: `checks: [Check]`, each `{id, state: ok \| warn \| fail, detail, fix: Fix \| null}`. |

`Check.id` is closed: `checkout_found`, `project_own_repository`, `settings_valid`,
`docker_reachable`, `agent_image` (`ok` cached, `warn` "will be built"), `evaluator_available`.
`Fix` is closed too (`choose_checkout`, `copy_out {suggested_path}`, `edit_setting {key}`,
`start_docker`), so every failure the UI shows carries an action the UI knows how to perform, and
"Start" is enabled exactly when no check is `fail`. `settings_valid` runs the same
`validate_run_request` the run itself runs, so a run that passes preflight does not fail on its
configuration seconds later; its errors name the key, as today.

`run.start` gains typed options behind feature `run_options`, replacing free text for what the
screen sets: `rounds`, `time_limit_s`, `results: keep | publish_github`, and
`roles: [{role, model | null, reasoning_effort: default | low | medium | high}]`, where each
role's model is one of the run harness's vetted models.
`args` stays for Advanced and may not repeat a typed option (`invalid_request` naming it).
Every role runs on the run's harness; a different harness per role is out of scope.

Machine statuses map from facts, not from a separate status field: `Sign in` when the SSH
connection needs a passphrase or key (the connection manager's state), `Checkout not found` when
`checkout.inspect` says so, otherwise `Ready`. Harnesses are assumed signed in; there is no login
flow.

### B4. Wait for quota and resume

**What exists.** On a usage limit the run pauses (`[agent.quota] policy = "pause"`, today's
default) and emits `quota_paused {provider, resets_at, resumes_at, ...}`; `command.resume` resumes
now; `policy = "wait"` waits for the reset automatically but is chosen before the run starts.

**Design.** `command.resume` gains `when: now | capacity_returns` (default `now`). With
`capacity_returns`, the run server arms the same capacity timer the `wait` policy uses (the
provider's `resets_at` plus slack, or `retry_seconds` polling when no reset time is known) and
emits `quota_waiting {resumes_at | null}`; `command.resume {when: now}` or `command.pause` cancels
the wait (`quota_wait_cancelled`). The wait is held by the live run server; if the server ends, the
run is ended and resumable like any other. The banner offers "Resume now" and "Wait and resume when
quota is back" and shows the expected time from `resumes_at`.

### B5. Run lists, ends and history

- **End record.** The ended instance record (tombstone) gains a typed `end` (record version bump):
  `outcome` (vs-core's `EndReason`: adopted, kept-input, no-winner, adoption-failed, stopped,
  deadline, finish-refused, plus `failed` and `force_stopped`), a one-line `reason`, `resumable`,
  and `resume_from_round`. The Run screen's ended banner and the sidebar's Recent rows read it.
- **Paused state in listings.** The live record gains `paused: user | quota | null`, written by the
  run server, so `instances.watch` can drive the sidebar's status and the "paused" notification
  without a run connection.
- **History for ended runs.** `runs.read_events {project, run_id, from_sequence, max}` behind feature
  `run_history` reads the run's recorded events from disk with a light journal reader, so an ended
  run opens with its map, overview and scrubber. The live path keeps `run.connect`.
- **Show all, Archive.** "Show all" lists `runs.list` per known project on each machine. Archive is
  app-local: the settings store records archived `(host, run_id)` pairs, which hides them from
  Recent; nothing on the host changes.

## Packages and screen stores

### New packages

All are `feature` packages marked strict:

| Package | Owns |
| --- | --- |
| `@vibesys/feature-app-shell` | The sidebar (New run, Live, Recent with Show all, Machines, Settings), screen navigation, the wordmark, and the shell layout. |
| `@vibesys/feature-new-run` | The New run screen and its store. |
| `@vibesys/feature-machines` | The Machines screen and its store. |
| `@vibesys/feature-run-page` | The Run screen: header and actions, startup timeline, scrubber, Overview, Map, Work, Logs, the quota and ended banners. |
| `@vibesys/feature-settings` | Settings → Appearance and Notifications; the effective-theme resolution. |

Each publishes `./state` (neutral) and `./renderer` (dom), and `./main` (node) where it answers
requests in the main process (`machines`, `new-run`, `settings`, `app-shell` for windows and
notifications). The desktop renderer gets a new composition entry (`shell.html`) that composes
them; it is marked strict too.

### Reused, not rebuilt

| Need | Reuse |
| --- | --- |
| Live run connection, bootstrap, reconnect | `@vibesys/run-session` (`WebSession`, the session machine) |
| Run event fold, rounds, performance | `@vibesys/run-state` (gains `./trace` and `./overview` folds for B1 and B2, and the campaign analytics moved down) |
| Logs: list, follow, fold | `@vibesys/logs` (`foldChunk`, the follow machine) and the connection's logs channel |
| Host identity, connection status, path rules | `@vibesys/hosts`; the path-completion reducer moves down from `feature-hosts` into it |
| Run ids, recent status, resume and stop policy | `@vibesys/runs` (the stop flow gains the force-stop offer) |
| Settings files | `@vibesys/settings` (gains appearance, notifications and archived runs) |
| Typed IPC | `@vibesys/ipc` (new channel groups per new feature) |
| Shell words for Advanced arguments | Moves from `feature-start-run/state` down to `@vibesys/runs` |

### Screen stores

One store per screen, per [Screen stores](../contributing/desktop-connection.md#screen-stores): a
discriminated union, a pure reducer with request tokens for data, and an XState machine where the
state is control state (work to start, wait for, and cancel). Each store has the invariants and
seeded-sequence property tests the Guards table requires.

**Sidebar** (`app-shell`, reducer over watches):

```text
SidebarState = {
  live: RunRow[]                       from instances.watch per host
  recent: {rows: RunRow[] /* ≤ 20, archived excluded */, more: number}
  machines: {count, attention: number}
  selection: {screen: 'new-run' | 'machines' | 'settings'} | {screen: 'run', host, run_id}
}
RunRow = {host, run_id, title, detail, status: live | paused | finished | failed | stopped}
```

**New run** (`new-run`, XState machine; the form is data inside it):

```text
NewRunState =
  | {kind: 'loading-machine', machine, token}
  | {kind: 'editing', machine: MachineFacts, project: ProjectChoice, tasks: {for: ProjectRef, list} | null,
     task, agent: AgentChoice, budget: Budget, advanced: string,
     checks: {for: Inputs, list: Check[]} | {for: Inputs, kind: 'checking', token}}
  | {kind: 'fixing', fix: Fix, token, editing: Editing}          copy out, create from example
  | {kind: 'starting', token, editing: Editing}
  | {kind: 'failed', failure, editing: Editing}
ProjectChoice = {source: 'recent', path} | {source: 'folder', path} | {source: 'example', example, dest}
AgentChoice = {harness, model /* ∈ vetted(harness) */, roles: {role, model /* ∈ vetted(harness) */ | 'same', effort}[] | 'same'}
```

Invariants: Start is enabled only in `editing` when the checks' `for` equals the current inputs and
none is `fail`; a task list for another project is not representable; the model is always one of
the selected harness's vetted models; changing an input re-runs preflight (debounced by the machine's
injected clock) and drops replies for older inputs.

**Machines** (`machines`, reducer):

```text
MachinesState = {
  machines: {host, checkout, status: 'ready' | 'checkout-not-found' | 'sign-in' | 'checking', facts: Facts | null}[]
  selected: host | null
  adding: null | {kind: 'editing', host, checkout, suggestions: {for: prefix, list} | null}
               | {kind: 'checking', host, checkout, token} | {kind: 'failed', reason}
  fixing: null | {host, checkout, token}
}
```

**Run** (`run-page`): the session and command parts are control state and stay in
`@vibesys/run-session` and the stop flow; the page store is a reducer over the folded run:

```text
RunPageState = {
  identity: {host, run_id}
  phase: {kind: 'starting', stages} | {kind: 'running'} | {kind: 'paused', by: 'user'}
       | {kind: 'paused', by: 'quota', quota: QuotaPause, waiting: {resumes_at} | null}
       | {kind: 'stopping', since, force_offered: boolean}
       | {kind: 'ended', end: EndRecord}
  view: {at: 'live'} | {at: Sequence}       the scrubber; 'live' follows new events
  tab: 'overview' | 'map' | 'work' | 'logs'
  map: {filter: 'all' | 'running' | 'waiting' | 'failed' | 'finished', open: Set<SpanId>, selected: SpanId}
  work: {query, outcome: 'all' | 'accepted' | 'rejected' | 'running'}
  logs: {selected: StreamId | null, following: boolean}
  confirm: null | 'stop'
}
```

Everything shown is a selector over (folded run at `view`, `RunPageState`): the Map (the span tree), key numbers,
chart, work rows and "Current work" are `selectX(fold(events ≤ view), state)`, so the scrubber needs
no second code path. Force stop is offered only in `stopping` after a bound (two minutes, from the
injected clock); the Stop confirmation's "what is lost" text is a selector over the spans (which
workstreams finish their step, which unfinished turns are discarded). Pause, Resume, Stop and
"Wait and resume" are commands on the existing run-control machine.

**Settings** (`settings`, reducer): `{mode: 'system' | 'light' | 'dark', light: LightTheme, dark: DarkTheme, notify: boolean, os: 'light' | 'dark'}`;
the effective theme is a selector, persisted by the settings store.

### Windows and notifications

- **Open in new window** (a run's "⋯" menu and its sidebar row menu) opens a new `AppWindow` bound
  to that run, using the per-run keying that already exists; ordinary navigation keeps replacing the
  current window's content. This settles the UX question in #2615 for v1.
- **Notifications.** Main raises an Electron `Notification` when a watched run moves to finished,
  failed or paused (from `instances.watch` and B5's `end` and `paused`), if enabled in Settings.
  Clicking it focuses the window showing the run, or opens one.

## PR order

Design system and enforcement first; backend contracts in parallel (they touch disjoint files),
with the run trace's `vs_core` work first on the backend track; screens after both. Each PR is independently mergeable and leaves CI green. Merge once CI passes.

| # | PR | Depends on |
| --- | --- | --- |
| 1 | **Tokens and themes.** `@vibesys/ui/tokens` (the six themes, type, space, radii, elevation, motion), the generator for `tokens.css` and its drift check (S9), bundled fonts, `contrastRatio` moved down, the contrast test (S10). Existing `@vibesys/ui` consumers keep working: the old variable names are aliased to the new roles until cutover. | none |
| 2 | **Enforcement.** The `design` marker; graph rules `strict-no-stylesheets`, `strict-imports`, `strict-lint-coverage`; Grit rules `strict-no-raw-elements` and `strict-no-raw-values` with their fixtures; `check_frozen_ui.mjs` and `FROZEN_UI_PACKAGES` (F1); docs in `clients-architecture.md`. | 1 |
| 3 | **Components, batch 1.** Layout and text primitives, `Button`, `SegmentedControl`, `Select`, `Field`/`TextInput`/`PathInput`, `Checkbox`/`Switch`, `Chip`, `StatusDot`, `Card`, `Banner`, `Icon`, `Wordmark`; `apps/ui-gallery`; visual and axe tests per theme (S11, S12) and the gallery coverage meta-test. | 1 |
| 4 | **Components, batch 2.** `Menu`, `Dialog` restyle, `Table`, `Tabs`, `TreeRow`, `KeyValue`, `CheckList`, `Timeline`, `LogPane`, `Scrubber`, `Stat`, `Chart`, `SidebarItem`, `SidebarSection`, `EmptyState`. | 3 |
| B-a | Helper `machine.describe` and `checkout.inspect`. | none |
| B-b | Helper `examples.list`, `project.create_from_example`, `project.copy_out`. | none |
| B-c | Helper `run.preflight` and `run.start` typed options (`run_options`). | none |
| B-d | Ended record `end`, live record `paused`, helper `runs.read_events` (B5). | none |
| T-1 | **Run trace in `vs_core`, runtime fields.** Spans recorded where transitions are journaled, deterministic ids, run-clock times in the journal, attempts from `ctx.retry`, failure and stop; the replay and structure properties. Its own reviewed PR. | none |
| T-2 | **Span labels in `vs_core`.** `SpanKind` declarations at `@search`, `ctx.label` and `label=` on aspect calls, key-naming rejection; `flows.md` updated. Its own reviewed PR. | T-1 |
| T-3 | **Trace on the wire.** `span_started`, `span_updated`, `span_ended`, snapshot spans, attribute schemas in the `ProtocolDocument`, log owners and usage records keyed by span id; the `run-state` `./trace` fold. | T-2 |
| T-4 | **Dynamic search labels.** Declares its kinds and labels planning calls, workstreams, agent turns and gates. | T-3 |
| B-e | Run overview contract (B2), with the `run-state` `./overview` fold. | none |
| B-g | `command.resume {when: capacity_returns}` and `quota_waiting` (B4). | none |
| 5 | **App shell and Settings.** `feature-app-shell`, `feature-settings`, `shell.html`, theme application and `nativeTheme`, notifications; behind a development switch (`VIBESYS_DESKTOP_SHELL=next`) until cutover. | 2, 4, B-d |
| 6 | **Machines.** `feature-machines`. | 5, B-a |
| 7 | **New run.** `feature-new-run`, including examples, copy out and preflight. | 5, B-a, B-b, B-c |
| 8 | **Run page, part 1.** Header and actions (Pause/Resume, Stop with confirmation, Force stop), startup timeline, Logs tab, ended banner, open in new window. | 5, B-d |
| 9 | **Run page, part 2.** Overview (objective, key numbers, chart, current work) and Work. | 8, B-e, T-4 |
| 10 | **Run page, part 3.** Map and the scrubber. | 9, T-4 |
| 11 | **Quota banner.** "Resume now" and "Wait and resume when quota is back". | 8, B-g |
| 12 | **Cutover.** The new shell becomes the only desktop renderer; the welcome view, Start sheet, Inspect sheet, the desktop's use of the web run page and the old features only the desktop used are deleted, and leave `FROZEN_UI_PACKAGES`. Packages still used by `apps/web` are deleted with it, in the web deprecation. | 6–11 |
| 13 | **Strict by default.** F2: strict rules apply to every package, the marker and `strict-lint-coverage` are removed, `FROZEN_UI_PACKAGES` is gone. | 12 and the web client's removal |

Each screen PR (5 to 11) carries screenshots in all six themes and a Playwright journey against
the fake helper and a fake run that reports startup stages and span events.

## Open questions

1. **Time limit and results publishing.** "Time limit" maps to the run's deadline and "Also publish
   to GitHub" to the existing publish settings; both need confirming against the current
   configuration keys in B-c.
2. **Font choice.** Schibsted Grotesk and JetBrains Mono come from the prototype. Both are OFL; if
   the user prefers system fonts to bundling, only `fonts.css` and the type table change.
