# Development guide

This page is the starting point for contributors extending VibeSys. It links
to the focused guides for framework code, domains, skills, profilers, input
bundles, and the desktop app.

## Before you change code

- Load the `software-design` skill for every code change and the `testing` skill for
  any test work (see [Coding best practices](coding-best-practices.md) for where they
  live and for size limits, lint waivers, and doc links). The
  [testing map](testing-map.md) says which layer a new test belongs in.
- Adding or moving an example? Register it in `examples/registry.toml`; see
  [Adding an example](examples.md).
- Keep changes within the owning package and preserve the framework boundaries.
- Use the repository [pull request template](https://github.com/uw-syfi/vibesys/blob/main/.github/pull_request_template.md)
  when opening a PR.

## Repository layout

```text
src/vibesys/             Orchestration policy and thin product API/composition
src/server/              Frontend-serving runtime and protocol
src/entrypoints/         Process composition and command entrypoints
clients/libs/contracts/      TypeScript server protocol types and validator
clients/libs/transport/      Runtime-neutral transport and the WebSocket adapter
clients/libs/run-state/      Pure backend-event projection (the run fold)
libs/                    Reusable standalone libraries
examples/                Candidate repositories, tasks, and input bundles
resources/evaluators/    Reusable versioned evaluator packages
resources/skills/        Bundled Agent Skills and reference material
resources/profilers/     Profiler MCP servers and support packages
docs/                    Contributor and subsystem guides
tests/                   Python and integration tests
```

The main framework boundaries are:

- `src/entrypoints/` owns executable composition. Entrypoints do not live in
  `libs/`.
- `src/server/` owns serving and frontend-specific behavior. It may depend on
  `src/vibesys/`, but the headless core does not depend on it.
- `src/vibesys/orchestration/` owns built-in orchestration plugins: agent
  roles, prompts, reply schemas, search and selection policy, evaluation
  cadence, and policy state. See
  [Orchestration plugins and runtime](orchestration-runtime.md).
- `src/vibesys/api/`, `src/vibesys/run/`, and
  `src/vibesys/composition.py` form the thin product facade and composition
  layer over the reusable runtime libraries.
- `libs/` owns reusable libraries. Import each library through its public
  `<package>.api` surface, for example `vs_agent.api` or `vs_project.api`.
  Each library exposes its owned fakes through `<package>.api.testing` where
  applicable. Orchestration policy normally uses
  `vs_runtime.api.testing.FakeRun`. Tach rejects imports of root-level
  exports and internal modules.
- `src/vibesys/domains/` owns domain-specific prompt policy.
  Generic execution mechanisms belong in `libs/vs-runtime/`; agent harnesses,
  compute isolation, and project persistence belong in `libs/vs-agent/`,
  `libs/vs-sandbox/`, and `libs/vs-project/`, respectively.
- Candidate repositories own target-specific tasks and candidate contracts
  below `.vibesys/tasks/`. Example projects live under `examples/<category>/`.
- `resources/evaluators/` owns reusable versioned evaluator packages.

## Local development

### Work from a source checkout

Install Python 3.12+, Git, and [uv](https://docs.astral.sh/uv/), then clone the
repository. Optional local credentials and configuration can be copied from the
provided examples:

```bash
cp .env.example .env
cp agent.toml.example agent.toml
```

`uv run` creates the Python environment automatically, so `uv sync` is not
required before running commands. For example:

```bash
uv run vibesys --help
uv run vibesys validate examples/data-structures/repositories/queue-rs --task spsc
uv run pytest
```

For desktop development, see [Desktop app development](desktop-development.md).

### Tests and submodules

Repository submodules are opt-in. Initialize them only when your work needs the
vendored sources, using `--checkout` to override their default update policy:

```bash
git submodule update --init --recursive --checkout
```

`uv run pytest` does not need any of them. The tests that assert on a
repository-native example's tasks skip when its submodule is absent, and CI's
`validate-examples` job covers them instead. To get the same coverage locally
without cloning the candidate repositories, fetch just their `.vibesys`
directories (a few MB and a few seconds, against hundreds of MB for a full
checkout):

```bash
uv run python scripts/example_repositories.py
```

A worktree often shows an example's submodule as modified, because the checked-out
commit is not the pinned one. Stage explicit paths, never `git add -A` or `git commit -a`
over `examples/`. A change that moves the pin of an `examples/*/repositories/*` submodule
fails `check_example_pointers` (the pre-push gate and CI's `changes` job) unless a commit
message of the change declares it with a line `Example-Bump: <path of the submodule>`, one
per pin. A declared bump also runs `validate-examples` in the pull request's CI instead of
waiting for the merge queue. To put an accidental pin back, run
`git checkout origin/main -- <path>`.

The example evaluators' build-and-run tests (they compile an example's native code and
run the real Go evaluator on it) carry the `native_build` marker and are deselected by
default. CI's `native-evaluator-examples` job runs them when an example or the evaluator
SDK changes; run them yourself with `uv run pytest -m native_build tests/architecture
tests/examples`.

Run the Python checks from the repository root:

```bash
./scripts/check_format.sh
./scripts/check_lint.sh
uv run pytest
```

For a focused test, use for example:

```bash
uv run pytest tests/vibesys/orchestration/issue_queue/test_stories.py
uv run pytest -k orchestrator
```

### Dynamic-loop smoke tier

Run `./scripts/smoke_dynamic_loop.sh` before every live hardware run. It
launches the installed `vibesys` CLI (launcher and engine) with `--outer-loop
dynamic --headless` against the Slurm run environment on the Fake cluster
(`vs_slurm.fake_connector` in executing mode), with real agent CLIs (Claude
Haiku by default; `VIBESYS_SMOKE_PROVIDER=codex` selects Codex). The input is a
small CPU task under `tests/e2e/dynamic_smoke/bundle`. A second scenario sends
Ctrl-C mid-run.

It checks loop invariants from the run's own records
(`tests/support/loop_invariants.py`): a typed terminal status and no empty
completion, every offered capability served or withdrawn after `unsupported`,
no MCP tool timeout, every run path a prompt names present at turn start, no
evaluation submitted after a stop, the stop grace bound, no Slurm job left
behind, and recorded token usage. Each run prints one summary line (wall time,
tokens, cost) to `smoke-summary.txt` under a fresh `vibesys-smoke-*` directory in `$TMPDIR` (or `/tmp`); set
`VIBESYS_SMOKE_DIR` to choose another location outside the checkout. One run
of both scenarios takes about 5 minutes and about $0.60 of Haiku tokens. It is
opt-in (`VIBESYS_E2E_AGENTS=1`) and not in PR CI, because real agents are
nondeterministic.

### Agent-less runs

An agent-less run drives the real backend (workspaces, builds, trusted evaluations, run state, every
event a frontend renders) with agents that are code, so it needs no model and costs no tokens. Use it
to exercise the desktop and web apps against live data, and to check a backend change end to end,
before spending a model on a real run. It supports the `dynamic` search only.

```bash
vibesys example new data-structures/repositories/queue-rs ~/queue-run
cd ~/queue-run
vibesys --task mpmc --outer-loop dynamic --agent-backend scripted --max-rounds 5 --max-in-flight 2
```

In the desktop app's New run form, open More options on the Review page and enter `--agent-backend scripted --max-rounds 5`
(any model chosen above is ignored). `[agent].scripted_seed = N` in `agent.toml` sets the seed
(default `0`): the same seed over the same project plans, edits and judges the same way, in any order
and across a resume, because each choice is a function of the seed and the hypothesis id alone.

The scripted planner schedules up to three ideas per turn, some built on an earlier candidate. Each idea
names a recipe, a real edit of `src/lib.rs` in the queue example: ones that speed the queue up, a neutral
one, one that slows it down, one that does not build and one that fails the trusted check. On top of that,
a share of implementer turns fail the way real ones do (gives up, nominates nothing, replies against the
schema, loses the turn, reports blocked) and a share of judged candidates are rejected. Five rounds take
a few minutes: the candidate is the cheap CPU queue, and each measurement runs about half a minute.
The decisions are pure and live in `src/vibesys/orchestration/dynamic/scripted/`; `src/launch/scripted_agents.py` reads and edits the workspace; the client is
`vs_agent.api.ScriptedAgentClient`, the one fake agent client configured for the run (the `stub` backend
only smoke-tests the interface and measures nothing).

Tests: `tests/vibesys/orchestration/dynamic/test_scripted_agents.py` checks the replies against the real
reply models without a process. `tests/e2e/test_scripted_run_e2e.py` is the real-tier check: it runs the
queue example through the production launcher on the executing Fake Slurm cluster (needs `cargo` and
`go`, skipped without them).

### Real-cluster tier

`./scripts/run_slurm_cluster_tests.sh` runs the `slurm` run environment, in four
configurations, against a real Slurm cluster started in Docker: munge, slurmctld,
slurmdbd (so `sacct` works), sshd and a login node in a `head` container, and one
slurmd with four fake GPUs (GRES backed by character devices, no real hardware)
in a `node` container. No LLM agents run. The agent is a real Docker container
that VibeSys creates exactly as in production, and the tests drive it with
scripted commands (`vibesys-gpu`) through the session's sandbox.

The test's run directory is mounted into both cluster containers at the same
absolute path and stands in for the shared filesystem. `slurm` reaches the head
over SSH with a per-session key and `ssh_command` options, so nothing in
`~/.ssh` is read or changed. The local transport needs a Slurm client on the host;
the tier points its `shell_command` at a small shim that runs the command line in
the head container (the login node) with the caller's working directory and
exactly the caller's environment, so the host installs nothing.

| Kind | Configuration | Tests |
|---|---|---|
| `slurm` | SSH transport | opening and closing, the agent surface, sockets |
| `slurm-local` | local transport, no agent GPU commands | the same, and that the container reaches no broker |
| `slurm-local-gpu` | local transport plus `[vibesys.agent_gpu]` | the same, plus GPU scheduling, cancellation, paths, job environment, confinement and `vibesys-gpu` |
| `slurm-gpu-alias` | the deprecated `--run-environment slurm-gpu` file | smoke: its deprecation notice and one GPU command |

Each kind is opened as a run would open it: the agents have the evaluation tool, so no gate,
and `test_agent_surface.py` checks that no evaluation command reaches the container.

The tier is marked `slurm_cluster` and skipped unless `VIBESYS_SLURM_CLUSTER=1`
and Docker are available. CI runs it as the `real-tiers-slurm` job when the `real_tiers` change filter selects it, on every merge-queue entry, and nightly. The script sets the variable,
removes everything it created on exit, and passes extra arguments to pytest.
The shared directory must be on a local filesystem: set
`VIBESYS_SLURM_CLUSTER_DIR` when `/tmp` is a network mount. The first run builds
two small images (Ubuntu with `slurm-wlm`, about a minute); later runs reuse
Docker's cache. Tests wait on Slurm's own state (a job running, the queue
empty) with a hang guard, never on a fixed delay.

What it does not cover: the ROCprof profiler transport, real GPU devices and
drivers, cgroup-based resource enforcement, multi-node jobs, and Landlock
confinement (the compute node uses bubblewrap).

### Minimal-container tier

`./scripts/run_minimal_container_tests.sh` starts every process that runs in the
agent's editor container, in a real container, on a host without GPUs. For each of
the CPU, CUDA and ROCm default base images (the ones named by the backends in
`libs/vs-sandbox/src/vs_sandbox/*_backend.py`) it opens the editor through
`open_run_environment`, so `docker build` of the real agent layer, the mounts, the
environment and the uid remap are all production's. The CUDA and ROCm images are
built and started without an accelerator; nothing runs on a GPU. No LLM is involved.
In each container it checks that:

- `claude --version` runs and `codex app-server` answers the `initialize` handshake;
- the evaluation tool server (both the core-run and the per-role descriptor) answers
  MCP `initialize` and lists its tools, started from the descriptor the run builds;
- the profiler MCP server of the base's default profiler answers `initialize` and
  lists its tools;
- `vibesys-gpu` runs a command through a real host
  broker (its `srun` is a local program);
- the `agent` user has a group of its own name and the host's uid and gid.

A fourth, small stand-in base (`ubuntu:24.04`, which owns uid 1000 as `ubuntu` and has
no `python` command) runs the tool-server and identity checks, so a host that cannot
pull the large images still covers them.

Tool servers do not use the base image's `python`: the agent image installs a uv-managed
Python at a fixed path, and the agent launcher starts every Python server on it with
the framework's source roots on `PYTHONPATH` (`vs_agent.containerize_server`).

The tier is marked `minimal_container` and skipped unless `VIBESYS_MINIMAL_CONTAINER=1`
and Docker are available. CI runs the CPU base as the `real-tiers-minimal-container` job when the `real_tiers` change filter selects it, on every merge-queue entry, and nightly. The script sets the variable, removes
the containers it started on exit (their run id starts with `minimal-container-`), and
passes extra arguments to pytest. Set `VIBESYS_MINIMAL_CONTAINER_BASES` (for example
`cpu,rocm`) to run some bases, and `VIBESYS_MINIMAL_CONTAINER_DIR` to a directory on a
local filesystem when `/tmp` is a network mount. A base image is pulled only when the
local store lacks it and is never removed: the CUDA and ROCm images are tens of
gigabytes each. With the images cached, one base takes about 15 seconds after its
agent layer is built, which takes minutes on the first run.

A test that fails for a known bug is marked `expect_failure_for(...)` with the
issue that tracks it. It is a strict expected failure, so fixing the bug fails the
tier until the marker is removed. Run with `--runxfail` to see the failures.

What it does not cover: GPU devices and drivers, profiler captures (only the server
starts), the accelerator-attached editor of the plain Docker environment on a GPU
host, and the SkyPilot and Modal editors.

The TypeScript client has its own workflow:

```bash
cd clients
pnpm install --frozen-lockfile
pnpm --dir contracts generate:protocol
pnpm check:ts-architecture
pnpm check:knip
pnpm check:clients
pnpm test:clients
pnpm build:clients
pnpm check:ts
```

`check:clients`, `test:clients`, and `build:clients` run through Turborepo: packages run in
dependency order and in parallel, and a task whose inputs are unchanged replays its cached result
from `.turbo/cache` at the repository root. Dev, tests, and type checks read workspace dependencies from source,
so no command needs another package built first; `pnpm turbo run test --filter @vibesys/<package>`
checks one package. The [clients architecture](clients-architecture.md) guide describes the build
model. `tsc -b` keeps each package's `dist` incrementally; `pnpm exec tsc -b --clean` in a package
removes it.

When Python protocol models change, regenerate the files under
`clients/libs/contracts/src/generated/` and review the diff.

The [desktop development guide](desktop-development.md) covers local and SSH
connections, the shell, and the Electron app.

## Extend VibeSys

Use the guide that matches the surface you are adding:

- [Add or customize a domain](domains.md) for new
  problem-space prompts, hooks, and domain registration.
- [Add or update Agent Skills](https://github.com/uw-syfi/vibesys/blob/main/resources/skills/README.md) and read the
  [VibeSys skill metadata guide](skill-metadata.md) when routing skills by
  backend or domain.
- [Extend profilers](extending-profilers.md) for profiler support packages,
  MCP tools, and profiler prompts.
- [Update CLI flags and combinations](../cli-flags.md) when changing the user
  facing command contract.

See the [agent sessions guide](agent-drivers.md) before changing how agents are
launched.

Keep target-specific APIs, ABIs, ownership rules, and service protocols in the
task's `CANDIDATE_CONTRACT.md` or design documentation rather than in the
neutral framework prompts.

## Internal workflows

For issue forms and repository issue conventions, see
[`docs/contributing/issue-authoring.md`](issue-authoring.md). For evolutionary search policy
work, see [`docs/contributing/openevolve.md`](openevolve.md).

## CI gates

Every pull request must pass the following gates before it can be merged.
You can run each one locally before pushing.

### Two stages

CI runs in two stages, in `.github/workflows/test.yml`, so the signal on a push is fast and
every test still runs before every merge:

| Stage | Event | What runs | Budget |
| --- | --- | --- | --- |
| Commit stage | `pull_request` | Change selection, format, lint, types, the Python repository-policy checks and guards (`python-checks`, `python-guards`), the Python tests the change reaches through imports (`test-impact`), the client quality, unit-test and build checks, the gateway and `repoctl` tests | warns at 270 s, fails at 360 s |
| Full stage | `merge_group`, `push` to `main` | The commit stage's checks and everything else: every Python test shard, the client end-to-end shards, seed exploration, coverage and patch coverage, the real-system tiers, the examples, the agent image and Feature tests, the sandbox jobs | warns at 270 s, fails at 600 s |

The merge queue runs the full stage on the tree the merge will produce, so a change that passes
its pull request and breaks the combination with something already queued is stopped there.
A job that belongs to the full stage only has `github.event_name != 'pull_request'` in its `if`.
`Required PR CI` waits for every job on both events (a skipped job counts as passed, a failed
one does not), and `ci-budget` checks the run's execution time against the stage's budget.
`tests/quality/test_ci_stages.py` fails when a job runs on pull requests and nowhere else, or when
`Required PR CI` does not wait for a job.

When a Python shard fails in the merge queue, `classify-failures` reruns only the tests it failed,
once, on the main the queue branched from, and labels each in its job summary: `fails on main
(flaky or broken main)` or `fails only with this PR`. It explains the failure and changes nothing:
the pull request's result is not retried and the queue still ejects it.

`test-impact` runs `scripts/impacted_tests.py` through the `python_impact_tests` check group: the
test files that import, directly or through other first-party modules, a module the change
touches, closest and cheapest first, within 100 s of tests on 4 workers. It reports how many
impacted files the budget left for the queue. The pre-push gate runs the same selection with a
30 s budget. A non-Python input a test reads (a golden, a template) selects nothing in the commit
stage; the full stage covers it.

The `changes` job reads `.repoctl/components.toml` for ownership, discovery,
and dependency policy, and `.repoctl/checks.toml` for executable check groups
and native commands. `support/repoctl/` provides configurable adapters for
language and package manifests. The component graph records cross-component
effects those manifests cannot express. The job prints each selection and its
reason; an unowned changed path fails selection instead of silently skipping
checks. To run the selected check groups configured for local runs, use one
command. The wrapper runs the repository's Go tool, so install Go 1.25 first:

```bash
./support/repoctl/repoctl test
./support/repoctl/repoctl test --checks-only   # quality groups and always-on scripts
```

`repoctl test` runs the groups whose `trigger_job` the change selects, both
the test groups and the quality groups (`python_quality`, `python_guards`,
`python_types`, `clients_quality`, `clients_build`), plus the scripts the
changes, doc-checks and history-checks jobs run on every pull request. `--checks-only`
keeps the quality groups and those scripts and leaves the test groups.
`--dry-run` lists either selection.

This does not run every CI check. A group that cannot run locally sets
`local_skip` and `--dry-run` prints the reason. Electron end-to-end tests
(`clients_e2e`) need a display, Chromium, and its system libraries as well as
the client and Python environments. The real-system tiers need Docker. Run
`clients_e2e` explicitly when desktop coverage is needed. Each Playwright run
asks the OS for a free port for its own server, so parallel runs on one machine
do not collide. CI splits the group across runners with `VIBESYS_E2E_SHARD=i/N`
(for example `2/5`), which each app's Playwright config reads; leave it unset
to run every spec. The desktop shell's specs run on four workers (fewer on a
smaller machine); `VIBESYS_E2E_WORKERS=<n>` names another count. A run that
writes screenshots and a contact sheet (`SCREENS_DIR` and the other capture
variables in `clients/tools/e2e/workers.mts`) builds each sheet from one
worker's screens, so it runs on one.

```bash
uv sync --dev
pnpm --dir clients install --frozen-lockfile
(cd clients && pnpm --filter @vibesys/app-desktop-main exec playwright install --with-deps chromium)
./support/repoctl/repoctl run-checks --group clients_e2e
```

A pull request runs the desktop specs its change can affect, not the whole suite, so a
change that breaks a spec is found before the merge queue (which runs every spec and
ejects the change and the entries behind it). `clients/tools/scripts/e2e_affected.mjs`
selects them from the path list: a changed spec runs; a changed helper runs the specs
that import it; a changed app file runs the app's specs; a changed workspace package runs
the specs that depend on it, through the dependencies of the spec's area
(`clients/tools/e2e/areas.json`, one entry per family of desktop-shell specs naming the
feature packages it exercises) and through the packages its imports reach. A spec the table
does not place depends on everything its app does, so an unplaced spec costs a run, never a
miss, and a lockfile change that only records what a workspace package asks for selects
nothing. The commit stage has a time budget (`ci-budget`), so a pull request runs the
selected specs up to `SPEC_BUDGET_SECONDS` of recorded time (about half the suite): a changed
spec always, then the specs importing a changed helper, then those of a changed app file,
then the specs a package change reaches, nearest first, each tier fastest first so more
distinct specs run. The log names the specs left to the queue, which runs every spec.
A job whose slice holds none of an app's specs skips that app's build and run (`with_e2e_specs.mjs`).
To see the selection for your branch:

```bash
node clients/tools/scripts/e2e_affected.mjs --mode selected --base origin/main --head HEAD
```

Place a new desktop-shell spec in the area it exercises by adding its file name to that
area's `specs`; the area table's tests fail on a spec two areas claim or a name that
matches no spec.

`clients_e2e` and the real-tier groups are the `local_skip` entries. Other
groups without `include_in_test` or `include_in_checks` are the CI form of a
suite the local command already runs, or they need artifacts only CI has.

### Client checks before a push

A change under `clients/` fails CI on checks that a package's own tests do not run: biome's
format check, knip, the TypeScript architecture checks, the frozen-UI check, and the gallery's
screenshots. One command runs them, as the same repoctl check groups CI runs:

```bash
./scripts/check_clients.sh            # clients_quality, clients_tests, ui_gallery_linux
./scripts/check_clients.sh --dry-run  # print the commands
```

The client tests need the Bun version pinned in `packaging/fetch_bun.py` (`BUN_VERSION`), the
one CI installs. `clients/tools/scripts/run_bun_tests.mjs` fails at once, naming the pin and
`packaging/fetch_bun.py`, when the Bun on `PATH` differs, because another version resolves
imports differently (Bun 1.3.9 cannot find `./welcome.cjs` from its `welcome.cts` source).

`ui_gallery_linux` compares the gallery's screenshots in the Linux image CI uses and checks
that two baseline updates write identical images, so it needs Docker. The script leaves out the
CI groups that need a display or build the client (`clients_build`, `clients_e2e`), each named
with its reason in the script.
`tests/quality/test_check_clients_matches_ci.py` fails when CI gains a client group the script
neither runs nor lists, so the two cannot drift.

Every push already runs a faster subset of these through the pre-push gate below.

### Pre-push gate

Every `git push` runs `repoctl gate` (`.githooks/pre-push`) and is refused when it fails. The gate
is the fast subset of what CI runs for the change, aimed at about 60 seconds on a laptop: the
format, lint, type and repository-policy checks, the Python tests the change can reach through
imports (`scripts/impacted_tests.py`, at most 30 seconds of tests on 4 workers), and for a change
under `clients/` biome, the architecture and unused-code checks, each package's checks, and the
tests of the packages the change touches and the packages that depend on them, in one Turborepo
run (the client tests need the Bun version pinned in `packaging/fetch_bun.py`, as above). Which checks run for which paths comes from the same
selection as `repoctl test`; each check group lists its gate commands as `gate_commands`
in `.repoctl/checks.toml`, and CI never reads them. The groups run
side by side and the gate prints each one's time.

```bash
./support/repoctl/repoctl gate --dry-run   # the commands the gate would run for this change
./support/repoctl/repoctl gate             # run it by hand
```

Nothing is installed by hand. The first repoctl command in a clone points the repository at the
tracked hooks with `git config core.hooksPath .githooks`; the setting lives in the repository's
own config, so it covers every linked worktree. When `core.hooksPath` already names another
directory repoctl leaves it and warns on every run that pushes are unchecked; call
`./support/repoctl/repoctl gate` from your own hook, or unset it. CI and runs that set
`REPOCTL_NO_HOOKS` leave the configuration alone.

The gate checks the working tree at `HEAD`, and its private-terms leg runs on every push (see
below). For an emergency, set the reason and push:

```bash
VIBESYS_SKIP_GATE='the reason' git push
```

The gate passes, prints that it was skipped, and appends the time, user, branch, commit and reason
to `vibesys-gate-skips.log` in the repository's git directory. CI and the merge queue still run
every check. `git push --no-verify` also skips the hook but leaves no record; use the variable.

### Private terms

Server names, internal hostnames and personal paths do not belong in the repository; use generic
placeholders such as `host-a` and `/data/vibesys`. `./scripts/check_private_terms.sh` fails when
tracked files, staged changes, or commits and messages not yet on `origin/main` match a term from a
private list. The list is a file of newline-separated, case-insensitive regular expressions (blank
lines and `#` comments are ignored), read from `$VIBESYS_PRIVATE_TERMS` or, by default,
`~/.config/vibesys/private-terms.txt`. Keep it outside every clone and never commit it. A missing
list makes the check print one line and pass, so it costs nothing on a machine without one.

The report lists `file:line` and never repeats the matched text, so its output is safe to paste.
The check cannot run in CI, and no CI job checks for these terms: the terms are private by design,
so a list CI could read would itself be the leak. It runs on your machine, from the pre-push hook.
`tests/quality/test_check_private_terms.py` covers the check with made-up terms.

Use `./support/repoctl/repoctl plan` to inspect the selection without running checks.
The workflow runs named check groups from `.repoctl/checks.toml`. Run
`./support/repoctl/repoctl verify-policy --cases tests/repoctl/cases.toml` after
changing component ownership or dependencies; CI runs this contract check before
selecting jobs.

### Format

```bash
./scripts/check_format.sh
```

Runs `ruff format --check` (whitespace, line length, blank lines) and
`ruff check --select I` (import order) across `src`, `tests`, `examples`,
`resources`, `libs`, and `support`. To auto-fix locally:

```bash
uv run ruff format src tests examples resources libs support
uv run ruff check --select I --fix src tests examples resources libs support
```

### Lint

```bash
./scripts/check_lint.sh
```

Runs `ruff check .` across the whole repository. Fix automatically where
possible with `--fix`; the remaining errors need manual attention. Test
files that trigger false-positive rules (e.g. `S106` on fixture arguments,
`ANN001`/`ANN201` on helpers, `PLR0913` on builder functions) can suppress
them at the site with `# noqa: <code>` only when reasonable lint-compliant
alternatives would make the design more hacky. List those alternatives and why
each is worse in the source rationale. Every suppression, including a
file-level `# ruff: noqa`, needs a `LW-` waiver ID and that rationale; see
[Ratchets](coding-best-practices.md#ratchets).

### Coverage

The test job enforces two independent coverage floors:

**Repo-wide floor — 75 %**  
`uv run pytest` (with `--cov` already wired in via `pyproject.toml`) must
reach 75 % combined statement + branch coverage across the tracked packages
(`entrypoints`, `server`, `vibesys`, `vs_agent`, `vs_bench`,
`vs_evaluator_protocol`, `vs_github`, `vs_issue_tracker`, `vs_core`,
`vs_project`, `vs_prompts`, `vs_runtime`, and `vs_sandbox`; the list is
`[tool.coverage.run] source` in `pyproject.toml`).

**Per-module floor — 40 %**  
`scripts/check_coverage_floor.py` reads `coverage.json` and rejects any
module below 40 % that is not in the `allowlist` in
`[tool.vibesys.per_module_coverage]` in `pyproject.toml`. The repo-wide
average can mask a single near-zero module; this floor prevents that (see
issue #298). Modules may be allowlisted only with a comment explaining why
they are intentionally untested.

**Patch coverage (PRs only)**  
New lines introduced by a pull request are checked separately against a
patch-coverage threshold. If your PR adds code that is not exercised by any
test, this gate will fail even if the repo-wide numbers are healthy. Add
focused unit tests for the new code paths — prefer pure-function extraction
(as in issue #290) to make new logic directly testable.

To reproduce the coverage check locally:

```bash
uv run pytest --cov-report=json
uv run python scripts/check_coverage_floor.py coverage.json
```
