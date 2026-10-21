# Agent Instructions

# Always follow

These rules apply to every code change. The module graph is in
[`docs/contributing/architecture.md`](docs/contributing/architecture.md).

Skills (load them, in every language):

- Before writing or changing any code, use the `software-design` skill
  (`.agents/skills/software-design/`): deep modules with declared public
  interfaces, one-way dependencies and data flow, and fitting a change to the
  existing design instead of extending a violating pattern. For modules,
  imports, interfaces and packages here, also use `change-module-graph`.
- Before writing, changing, or reviewing any test, use the `testing` skill
  (`.agents/skills/testing/`). Tests exercise public APIs only, use Fakes
  instead of patching or mocks, favor property-based tests, and are never flaky
  (a test's result must never depend on timing; every wait needs a bound that
  only guards against hangs). Also use `write-vibesys-tests` for this
  repository's harness. Deterministic-tier tests and product code use no
  real time, threads, processes, sockets, or signals outside `vs_sim`. Ruff
  `TID251` bans time; `tests/quality/test_real_apis_confined.py` covers
  threads, processes, sockets, and signals. Only the real tiers
  (`sim_real_tiers`: `tests/e2e`, `tests/slurm_cluster`,
  `tests/minimal_container`) drive real systems.
- Prefer a type, lint rule or check over a written rule; a written rule names
  its enforcement or says why there is none (the `enforce-rules` skill). All
  skills are indexed in [`.agents/skills/README.md`](.agents/skills/README.md);
  change them with the `write-skills` skill.

Architecture:

- Product behavior lives in `src/vibesys/` and is reached through `vibesys.api`.
  The generic pure lifecycle lives in `libs/vs-core/`, through `vs_core.api`.
  `src/headless/` and `src/server/` are peers over that facade; neither imports
  the other. Import each `libs/` package through its `<package>.api` only.
- Data flows one way: inputs go through core to typed outputs or events that
  consumers interpret. Core never imports adapters, renderers, or the CLI, and
  never branches on a concrete sandbox or backend name outside the wiring code.
  Backends emit semantic event data; formatting, colors, and layout belong to
  frontends.
- TypeScript clients: control state is an XState machine, data folds are plain
  reducers; see "State" in
  [`docs/contributing/clients-architecture.md`](docs/contributing/clients-architecture.md).
- Follow the package layout and placement rule in [architecture.md](docs/contributing/architecture.md).
- Design stateful systems with a pure core and a thin shell. Follow
  [Functional core, interfaces and implementations](.agents/skills/software-design/references/functional-core.md)
  for durable intent, I/O ownership, and interchangeable implementations.
  Interfaces are role-named `typing.Protocol`s owned by the I/O library;
  implementations are `<Variant><Role>` and pass the interface's contract suite.
- A new cross-module import needs its `tach.toml` edge in the same PR. Never add
  an upward edge or a cycle; prefer removing edges.
- Open the `.vibesys` layout through `vs-project`'s `Project`; do not rebuild
  paths.
- Agent prompts are rendered from templates: Python passes data, never
  concatenates or appends prompt text.

Data and validation:

- Anything that crosses a process or is persisted is a `.proto` message, validated
  with protovalidate in the same file; the `.proto` is the single source of truth.
  Existing Pydantic contracts migrate during the transport migration
  ([run-transport.md](docs/contributing/run-transport.md)); a new contract starts as a `.proto`.
  Use `StrEnum` or `Literal` for closed sets. No raw strings where an enum or registry exists.
- Never commit private server names, internal hostnames, personal paths, or keys and tokens;
  use generic placeholders such as `host-a`. No repository check can hold this, because the
  names themselves are private.
- Reject unknown config, metadata, and routing keys. Validate early with errors
  that name the offending key or path.
- Keep one source of truth per fact, and one authoritative definition of each
  cross-process contract with downstream types generated from it.
- No implicit fallback for agent-visible contracts unless documented and tested.

Checks:

- A bug fix needs a regression test that fails at the merge base and passes at
  the head. Sweep for other places with the same flaw, and make the test cover
  the input space where practical, not only the reported case.
- Treat lint suppressions as explicit opt-outs. Add one only after considering
  reasonable lint-compliant alternatives, and explain in the source comment why
  each would make the design more hacky than keeping the current code.
- Run `./scripts/check_format.sh`, `./scripts/check_lint.sh`, and only the
  checks and tests the change can affect; CI runs the rest on the PR. A docs- or
  skills-only change runs no test suite. `./support/repoctl/repoctl test` runs
  the checks CI would select for the change. Size limits and lint-waiver mechanics
  are in
  [`docs/contributing/coding-best-practices.md`](docs/contributing/coding-best-practices.md).

When preparing a pull request, use the repository PR template at
[`.github/pull_request_template.md`](.github/pull_request_template.md). Fill in
the `Problem`, `Solution` (including its `Design` answers), and `Verification`
sections, including correctness properties and testing details where relevant.

Keep changes narrowly scoped to the requested behavior, preserve existing
architecture boundaries, and run the smallest relevant checks before handing
work back.

Before using or adding the word "sandbox", or touching confinement, read
[`docs/contributing/sandboxing.md`](docs/contributing/sandboxing.md): it fixes
the vocabulary (run environment, command runner, agent confinement, candidate
isolation, trust boundary) and says what is enforced where.

Before touching the agent CLI integration, read "Where agentshim lives" in
[`docs/contributing/agent-drivers.md`](docs/contributing/agent-drivers.md): it
says which repository owns provider knowledge and which owns driver policy.
