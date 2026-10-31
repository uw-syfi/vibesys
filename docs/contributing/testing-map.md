# Testing map

Where each kind of guarantee is tested, how, and where a new test goes. The rules for
writing a test (public APIs only, Fakes instead of mocks, no flaky waits) are in the
`testing` skill (`.agents/skills/testing/`); this page is the map.

## The one rule

A guarantee is checked once, at the cheapest layer that can observe it. A rule about a
pure function is a property on that function, not a run of the whole system. The whole
system is run only to check that its parts are joined correctly, and then once, not once
per assertion. Layers meet at typed interfaces, so proving each side of an interface is
enough to trust the join.

## The layers

| Layer | What it guarantees | How it tests | PR budget (CPU-s) |
| --- | --- | --- | --- |
| `libs/*/tests` | Each library's own rules. In `vs-core`: budgets, admission, closure, settlement, stop, recovery to the crash-free result, flow restarts. In the I/O libraries (`vs-runtime`, `vs-project`, `vs-slurm`, ...): every implementation, Fake and real, satisfies its interface. | Properties over generated inputs, toy searches, one contract suite per interface that the Fake and the real implementation both run. | about 450 for all libraries |
| `tests/system` | The shell joins core to I/O safely: intent is written before the effect, a new host recovers a dead one, fencing and leases, a crash at each kind of boundary converges, delivery faults (lost, repeated, reordered), time limits on a virtual clock, a stop reaches a terminal state. | The skeleton search only (`tests/support/skeleton_*`), Fakes, a virtual clock. Adding a search adds no system test. | about 120 |
| `tests/vibesys` | Product behavior (the profiler tool-server tests live in `tests/vibesys/loops` for now; the SkyPilot adapter's tests are in `libs/vs-sandbox/tests`). For each search, that it chooses well (stories on a `FlowRun`, which also replays each story with a restart after steps), and the pure helpers around it. Product wiring: config and launch validation, one smoke run per search through `vibesys.api`, CLI and exit codes. | `FlowRun` stories, properties of pure helpers, one `vs-dst` world run per search. Search tests never write crash, restart or fault code. | about 250 |
| `tests/architecture`, `tests/quality`, `tests/server`, `tests/entrypoints`, `tests/examples`, `tests/support` | Static shape (imports, layout, prompts, docs links), repository policy ratchets, server and entry-point behavior, example registration, the test plumbing itself. | Source and tree inspection, small in-process checks. | about 150 |
| `tests/e2e` | A few real processes end to end (CLI, signals, confinement, git, containers). | One real call per entry point. Skipped unless the opt-in variable is set where it needs real credentials. | about 60 |
| real tiers: `tests/slurm_cluster`, `tests/minimal_container`, `real_contract` tests | Real Slurm, real containers, real databases. | Opt in with `VIBESYS_SLURM_CLUSTER=1`, `VIBESYS_MINIMAL_CONTAINER=1`, `VIBESYS_REAL_CONTRACTS=1`. Outside the pull request budget. | none |

The budgets are summed per-test CPU at PR depth with coverage on, and they are what keeps
the pull request workflow short. A single test should cost under 0.5 CPU-s at PR depth;
a test above that (a real process, a real git repository, a whole-system run) needs a
reason in a comment.

## PR depth and nightly depth

`uv run pytest` runs at `--depth=pr`, the default: property tests draw a few examples
(the Hypothesis `ci` profile, about 10, derandomized), crash and restart sweeps try a
sample of their points, and every pinned past bug or planted fault is always replayed.
`--depth=nightly` explores: more examples, every crash point, every restart step, and
the tests marked `slow`. The nightly workflow runs it. A pull request still runs one
case of each `slow` test under `tests/system/chaos` and `libs/vs-core/tests` (the first
case not marked `xfail`; the directories are the `sim_smoke_slow` setting), so a
regression only a slow test catches fails the pull request that causes it. Run it yourself when you change
recovery, the shell, or a contract suite:

```bash
uv run pytest libs tests/system tests/vibesys --depth=nightly -n 16 --no-cov
```

An example count above the default needs a reason; use `vs_sim.api.testing.examples(pr=..., nightly=...)`
to state both depths.

## Where a new test goes

New tests go next to the package they test. The top-level `tests/` holds only cross-package
layers (system, vibesys product wiring, e2e, architecture, quality, and the real tiers) and
is not to grow: no new top-level directories, and no new files there for a single package's
tests or helpers. Tests never go inside a tree that ships wholesale: `resources/profilers`,
`resources/evaluators` and `skills/domains` are grafted into the wheel and staged for
agents, so a test, conftest or test helper there would ship.

1. A rule about a pure function or a core step (a limit, an ordering, a state transition):
   a property next to it, in the owning `libs/*/tests` or beside the product module.
2. A new implementation of an I/O interface: add it to that interface's contract suite
   in the owning library; do not write implementation-specific tests of the same rules.
3. A search choosing differently (which parent, which plan, when to stop): a `FlowRun`
   story in `tests/vibesys/orchestration/<search>/`.
4. The shell, a crash, a fault or a time limit: `tests/system`, on the skeleton search.
5. Configuration, launch or CLI wiring: `tests/vibesys`, through `vibesys.api`.
6. A real process, signal or credential: `tests/e2e` or a real tier, one test per entry point.
7. A bug fix: a test that fails without the fix, in the lowest layer that reproduces it,
   plus a property that covers the neighbouring input space.

If a guarantee seems to need a test that multiplies layers (every search times every crash
point), a property or contract is missing at a lower layer. Add it there.
