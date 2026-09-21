# Development guide

This page is the starting point for contributors extending VibeSys. It links
to the focused guides for framework code, domains, skills, profilers, input
bundles, and the TUI.

## Before you change code

- Read [`docs/contributing/coding-best-practices.md`](coding-best-practices.md).
- Adding or moving an example? Register it in `examples/registry.toml`; see
  [Adding an example](examples.md).
- Keep changes within the owning package and preserve the framework boundaries.
- Use the repository [pull request template](https://github.com/uw-syfi/vibesys/blob/main/.github/pull_request_template.md)
  when opening a PR.

## Repository layout

```text
src/vibesys/             Headless optimization core and loop implementation
src/server/              Frontend-serving runtime and protocol
src/entrypoints/         Process composition and command entrypoints
clients/backend-client/  TypeScript server protocol and transport
clients/core-state/      Pure backend-event projection
clients/tui/             TypeScript terminal UI and launcher
libs/                    Reusable standalone libraries
examples/                Candidate repositories, tasks, and legacy input bundles
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
- `src/vibesys/loops/` owns the outer-loop policies and shared loop helpers.
- `libs/vs-agent/` (the `vs_agent` package) owns the agent-runner abstraction and integrations.
- `src/vibesys/domains/` owns domain-specific prompt context and hooks.
- `src/vibesys/backends/` owns compute and execution backends.
- Candidate repositories own target-specific tasks and candidate contracts
  below `.vibesys/tasks/`. Legacy input bundles remain under `examples/`.
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
uv run pytest tests/vibesys/loops/plain/test_plain_loop.py
uv run pytest -k orchestrator
```

The TypeScript client has its own workflow; see
[`clients/tui/README.md`](https://github.com/uw-syfi/vibesys/blob/main/clients/tui/README.md). The short version is:

```bash
cd clients
pnpm install --frozen-lockfile
pnpm --dir backend-client generate:protocol
pnpm check:ts-architecture
pnpm check:clients
pnpm test:clients
pnpm build:clients
pnpm check:ts
```

When Python protocol models change, regenerate the files under
`clients/backend-client/src/generated/` and review the diff.
See the [TUI architecture guide](tui-architecture.md) for package ownership and dependency rules.
TUI-specific contributor docs are indexed in [`tui/README.md`](tui/README.md).

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
- [Update feature flags](feature-flags.md) for opt-in
  experiments and optional framework behavior.

The experimental Omnigent adapter is a developer-facing alternative to the
standard CLI adapter. It currently supports Claude and Codex on the host path
only; see the [agent driver guide](agent-drivers.md) before enabling it.

Keep target-specific APIs, ABIs, ownership rules, and service protocols in the
task's `CANDIDATE_CONTRACT.md` or design documentation rather than in the
neutral framework prompts.

## Internal tools and workflows

The plain loop's issue MCP server is an internal development tool:

```bash
uv run vibesys-issue-mcp
```

For issue forms and repository issue conventions, see
[`docs/contributing/issue-authoring.md`](issue-authoring.md). For evolutionary search policy
work, see [`docs/contributing/openevolve.md`](openevolve.md).

## CI gates

Every pull request must pass the following gates before it can be merged.
You can run each one locally before pushing.

### Format

```bash
./scripts/check_format.sh
```

Runs `ruff format --check` (whitespace, line length, blank lines) and
`ruff check --select I` (import order) across `src`, `tests`, `examples`,
`resources`, and `libs`. To auto-fix locally:

```bash
uv run ruff format src tests examples resources libs
uv run ruff check --select I --fix src tests examples resources libs
```

### Lint

```bash
./scripts/check_lint.sh
```

Runs `ruff check .` across the whole repository. Fix automatically where
possible with `--fix`; the remaining errors need manual attention. Test
files that trigger false-positive rules (e.g. `S106` on fixture arguments,
`ANN001`/`ANN201` on helpers, `PLR0913` on builder functions) can suppress
them with a file-level `# ruff: noqa: <codes>` comment at the top of the
file.

### Coverage

The test job enforces two independent coverage floors:

**Repo-wide floor — 75 %**  
`uv run pytest` (with `--cov` already wired in via `pyproject.toml`) must
reach 75 % combined statement + branch coverage across the tracked packages
(`vibesys`, `vs_evaluator_protocol`, `vs_feature_flags`, `vs_github`,
`vs_issue_board`, `vs_loop_state`, `vs_project`, `vs_sandbox`, and `vs_bench`).

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
