# Development guide

This page is the starting point for contributors extending VibeSys. It links
to the focused guides for framework code, domains, skills, profilers, input
bundles, and the TUI.

## Before you change code

- Load the `software-design` skill for every code change and the `testing` skill for
  any test work (see [Coding best practices](coding-best-practices.md) for where they
  live and for size limits, lint waivers, and doc links).
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
clients/backend-client/  TypeScript server protocol and transport
clients/core-state/      Pure backend-event projection
clients/tui/             TypeScript terminal UI and launcher
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
  below `.vibesys/tasks/`. Input bundles remain under `examples/`.
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

To use the interactive TUI from a checkout, install Node.js 20+, Bun, and pnpm
11 (or enable Corepack). The launcher installs frontend dependencies and builds
the client when needed.

For a headless installation directly from GitHub without a checkout:

```bash
python -m pip install "git+https://github.com/uw-syfi/vibesys.git"
```

GitHub source installs skip optional submodules and do not build the native TUI.
Pass `--headless` to suppress the fallback notice. Use a supported PyPI wheel
when you need the bundled TUI.

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

Run the Python checks from the repository root:

```bash
./scripts/check_format.sh
./scripts/check_lint.sh
uv run pytest
```

For a focused test, use for example:

```bash
uv run pytest tests/vibesys/orchestration/issue_queue/test_plugin.py
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

### Real-cluster tier

`./scripts/run_slurm_cluster_tests.sh` runs the `slurm` and `slurm-gpu` run
environments against a real Slurm cluster started in Docker: munge, slurmctld,
slurmdbd (so `sacct` works), sshd and a login node in a `head` container, and one
slurmd with four fake GPUs (GRES backed by character devices, no real hardware)
in a `node` container. No LLM agents run. The agent is a real Docker container
that VibeSys creates exactly as in production, and the tests drive it with
scripted commands (`vibesys-gpu`, `vibesys-gate`) through the session's sandbox.

The test's run directory is mounted into both cluster containers at the same
absolute path and stands in for the shared filesystem. `slurm` reaches the head
over SSH with a per-session key and `ssh_command` options, so nothing in
`~/.ssh` is read or changed. `slurm-gpu` needs a Slurm client on the host; the
tier points its existing `srun_command` and `scancel_command` at a small shim
that runs them in the head container (the login node) with the caller's working
directory and exactly the caller's environment, so the host installs nothing.

The tier is marked `slurm_cluster` and skipped unless `VIBESYS_SLURM_CLUSTER=1`
and Docker are available. It is not in PR CI. The script sets the variable,
removes everything it created on exit, and passes extra arguments to pytest.
The shared directory must be on a local filesystem: set
`VIBESYS_SLURM_CLUSTER_DIR` when `/tmp` is a network mount. The first run builds
two small images (Ubuntu with `slurm-wlm`, about a minute); later runs reuse
Docker's cache. Tests wait on Slurm's own state (a job running, the queue
empty) with a hang guard, never on a fixed delay.

What it does not cover: the ROCprof profiler transport, real GPU devices and
drivers, cgroup-based resource enforcement, multi-node jobs, and Landlock
confinement (the compute node uses bubblewrap).

The TypeScript client has its own workflow; see
[`clients/tui/README.md`](https://github.com/uw-syfi/vibesys/blob/main/clients/tui/README.md). The short version is:

```bash
cd clients
pnpm install --frozen-lockfile
pnpm --dir backend-client generate:protocol
pnpm check:ts-architecture
pnpm check:knip
pnpm check:clients
pnpm test:clients
pnpm build:clients
pnpm check:ts
```

Run only one client build, check, or test command at a time in a checkout. These commands may
rebuild runtime workspace dependencies, and each build deletes its shared `dist` before writing
the replacement. `--workspace-concurrency=1` orders packages within one pnpm process; it cannot
coordinate a second process in the same checkout. Use a separate Git worktree when client commands
must run concurrently.

An overlap commonly fails with either `Cannot find module '@vibesys/core-state'` or
`Cannot find module '@vibesys/backend-client'`. A late overlap can instead produce
`Incomplete test run: ... test files reported no tests`. Stop the overlapping command, run
`pnpm build:clients`, then rerun the failed command. The build is deliberately not changed to
preserve the old `dist`: clearing it prevents renamed or deleted source files from surviving as
stale JavaScript or declarations.

When Python protocol models change, regenerate the files under
`clients/backend-client/src/generated/` and review the diff.
See the [TUI architecture guide](tui-architecture.md) for package ownership and dependency rules.
TUI-specific contributor docs are indexed in [`tui/README.md`](tui/README.md).

The [web UI development guide](web-development.md) covers replay mode, live
WebSocket smoke runs, detached gateway lifecycle, and SSH access from a local
laptop to a remote VibeSys host.

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
```

This does not run every CI check. Browser end-to-end tests (`tui_e2e`) are
CI-only by default: Playwright needs Chromium and its system libraries as well
as the client and Python environments, so making the normal local command
acquire or require them would make it unexpectedly heavyweight. Run that group
explicitly when browser coverage is needed. Local port 5173 must be free or
already serve this checkout's web app: Playwright reuses a local listener, so
an unrelated listener would run the wrong app.

```bash
uv sync --dev
pnpm --dir clients install --frozen-lockfile
(cd clients && pnpm --filter @vibesys/web exec playwright install --with-deps chromium)
./support/repoctl/repoctl run-checks --group tui_e2e
```

The CI-only decision is specific to `tui_e2e`. It does not change the
selection policy for the other check groups without local-selection keys; each
has its own prerequisites and needs its own policy decision.

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
