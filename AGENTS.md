# Agent Instructions

# Always follow

These rules apply to every code change. The full reference, with rationale and
lint-waiver mechanics, is
[`docs/contributing/coding-best-practices.md`](docs/contributing/coding-best-practices.md).

Architecture:

- Core behavior lives in `src/vibesys/` and is reached through `vibesys.api`.
  `src/headless/` and `src/server/` are peers over that facade; neither imports
  the other. Import each `libs/` package through its `<package>.api` only.
- Data flows one way: inputs go through core to typed outputs or events that
  consumers interpret. Core never imports adapters, renderers, or the CLI, and
  never branches on a concrete sandbox or backend name outside the wiring code.
- Backends emit semantic event data. Formatting, colors, and layout belong to
  frontends.
- A new cross-module import needs its `tach.toml` edge in the same PR. Never add
  an upward edge or a cycle; prefer removing edges.
- Open the `.vibesys` layout through `vs-project`'s `Project`; do not rebuild
  paths.

Data and validation:

- Use Pydantic models for external contracts and `StrEnum` or `Literal` for
  closed sets. No raw strings where an enum or registry exists.
- Store one source of truth per fact. Do not add a field that another field
  determines.
- Reject unknown config, metadata, and routing keys. Validate early with errors
  that name the offending key or path.
- No implicit fallback for agent-visible contracts unless documented and tested.
- Keep one authoritative definition of each cross-process contract and generate
  downstream types from it.

Testing and checks:

- Before writing, changing, or reviewing any test, use the `testing` skill
  (`.agents/skills/testing/`), in every language. Tests exercise public APIs
  only, use Fakes instead of patching or mocks, favor property-based tests, and
  are never flaky (no reliance on timeouts, sleeps, or wall-clock time).
- A bug fix needs a regression test that fails at the merge base and passes at
  the head.
- Run `./scripts/check_format.sh`, `./scripts/check_lint.sh`, and the narrowest
  relevant `uv run pytest` target before handing work back.

When preparing a pull request, use the repository PR template at
[`.github/pull_request_template.md`](.github/pull_request_template.md). Fill in
the `Problem`, `Solution`, and `Verification` sections, including correctness
properties and testing details where relevant.

Keep changes narrowly scoped to the requested behavior, preserve existing
architecture boundaries, and run the smallest relevant checks before handing
work back.

Before touching the agent CLI integration, read "Where agentshim lives" in
[`docs/contributing/agent-drivers.md`](docs/contributing/agent-drivers.md): it
says which repository owns provider knowledge and which owns driver policy.

# Delegation

The main agent does orchestration: planning high-level effort, monitoring
progress, and steering. Delegate detailed work (searching, reading, and
editing across files) to subagents. Use cheaper models whenever the task
allows, and run independent subagents in parallel.

Before creating a GitHub issue, read
[`docs/contributing/issue-authoring.md`](docs/contributing/issue-authoring.md) and the matching form under
`.github/ISSUE_TEMPLATE/`. Search both the codebase and open and closed issues
before filing. Use the repo-local `create-issue` skill when it is available.

For `resources/skills/serving-systems/`, also follow the subtree-specific
authoring guide in
[`resources/skills/serving-systems/CLAUDE.md`](resources/skills/serving-systems/CLAUDE.md).

# Writing style

Applies to both chat replies and docs.

- Be concise. Prefer the shortest version that is still precise. Cut preamble,
  recaps, and restating the question.
- Do not over-explain or hand-hold. Assume the reader knows generic CS.
- Use precise technical CS terms; keep them. Drop inflated or
  self-congratulatory jargon and piled-up metaphors (e.g. "blast radius", "earn
  their keep", "the frontier is the feature"). Say the plain precise thing
  instead.
- Direct engineering voice: lead with the answer, then support it.
- Tables and short code are fine when they carry information; keep them small.
  Put exhaustive detail in an appendix, not at the top of a doc.
- No em dashes. Use commas, colons, parentheses, or separate sentences instead.
