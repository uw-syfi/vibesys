# VibeSys Review Guide

Use the cross-cutting sections for every review, then apply only the sections
matching the changed surfaces.

## Contents

- [Repository Ownership And Architecture](#repository-ownership-and-architecture)
- [Correctness, Failure, And Lifecycle](#correctness-failure-and-lifecycle)
- [Contracts And Compatibility](#contracts-and-compatibility)
- [Python 3.12](#python-312)
- [TypeScript TUI](#typescript-tui)
- [Go, Rust, C, And C++ Evaluators](#go-rust-c-and-c-evaluators)
- [Configuration, Features, And Generated Files](#configuration-features-and-generated-files)
- [Prompts, Templates, And Agent Skills](#prompts-templates-and-agent-skills)
- [Documentation And Example Drift](#documentation-and-example-drift)
- [Tests And Evidence](#tests-and-evidence)

## Repository Ownership And Architecture

- Keep framework behavior under `src/vibesys/` and behavior owned by a loop,
  domain, backend, renderer, or agent in that package.
- Put reusable standalone Python capabilities under `libs/` with a focused
  public interface and their own tests. Prefer the canonical existing module
  when behavior has one owner; do not create a package solely to split a small
  function or anticipate reuse.
- Keep example-specific evaluator, checker, benchmark, and reference behavior
  in its standard example bundle. Do not move one workload's policy into the
  framework or create shared infrastructure merely to set up an example.
- Keep runtime prompt assets under `src/vibesys/prompts/`, organized by loop,
  domain, and backend. Keep executable domain hooks and definitions in
  `src/vibesys/domains/`, separate from their prompt context.
- Keep long-form serving knowledge under `resources/skills/`. Follow
  `resources/skills/serving-systems/CLAUDE.md` for that subtree.
- Preserve unidirectional data flow: stable interfaces and typed values feed
  implementations, which emit semantic results for consumers. Core behavior
  must not reach back into concrete sandboxes, renderers, clients, or
  application defaults.
- Separate shared interfaces, application configuration, implementation, and
  wiring when they have different owners or reasons to change. Configuration
  should normally register a new case rather than force concrete-type branches
  through implementations.
- Keep compatibility wrappers thin and behavior in the canonical module.

Ask of new abstractions:

1. What contract or ownership boundary does this protect?
2. Which independent consumers reuse it now?
3. Does its dependency direction stay one-way?
4. Can the behavior remain simpler in an existing owner?

## Correctness, Failure, And Lifecycle

- Exercise empty, malformed, duplicate, boundary-size, partial, retry,
  timeout, cancellation, and concurrent inputs where the code can encounter
  them.
- Validate external inputs early and report the offending path, key, flag,
  backend, or field. Reject unknown config and routing keys rather than
  silently ignoring them.
- Check cleanup on success, failure, timeout, and cancellation. The component
  that creates a sandbox, subprocess, task, thread, temporary resource, or
  subscription owns deterministic and preferably idempotent cleanup.
- Look for partial state commits, unsafe retry behavior, check-then-act races,
  non-atomic persistence, stale caches, unbounded queues, and lost exceptions.
- Treat subprocesses as integration boundaries: use argument arrays, explicit
  working directories/environment/encoding/timeouts, actionable domain errors,
  injectable runners when useful, and redacted diagnostics.
- Verify security boundaries: path traversal, command injection, unsafe
  deserialization, secret exposure, untrusted prompt or template interpolation,
  over-broad filesystem/network access, and trust decisions based on user input.
- Flag any new state field whose value is determined by other fields in the
  same model, and ask for a selector or predicate instead. Duplicate state
  needs every writer to stay in sync, and the reviewed diff usually updates
  only the writer it touched.

## Contracts And Compatibility

- Keep one authoritative definition for cross-process and cross-language
  contracts and generate downstream representations when practical.
- Prefer additive, backward-compatible evolution. Require an explicit version
  or compatibility boundary for incompatible protocol changes.
- Review serialized forms, defaults, optional-versus-missing semantics, enum
  exhaustiveness, ordering, identifiers, and error representations.
- Test representative payload round trips and every affected consumer, not only
  the producer. Backend events remain semantic; clients own presentation,
  formatting, truncation, colors, and layout.
- Put nontrivial candidate-facing APIs, ABIs, ownership rules, and service
  protocols in `CANDIDATE_CONTRACT.md`; keep evaluator internals and trust-model
  rationale in design docs.

## Python 3.12

- Expect typed Python that passes `ty` (`./scripts/check_types.sh`). Avoid `Any` or casts that
  hide an uncertain boundary; preserve type information through registries,
  callbacks, serialization, and async code.
- Use Pydantic models for configuration, metadata, persisted state, structured
  agent output, and other external contracts. Prefer `StrEnum`, `Literal`, and
  typed registries for closed sets; use small immutable dataclasses for internal
  values when runtime validation is unnecessary.
- Normalize `Path`-like inputs once at a boundary. Check path containment and
  platform behavior rather than relying on string prefixes.
- Review async code for blocking calls, orphaned tasks, cancellation handling,
  double completion, and exception retrieval. Review context managers and
  generators for cleanup across exceptional exits.
- Favor observable-contract tests with Pytest/Hypothesis over private call
  structure. When relevant, run focused tests, `./scripts/check_format.sh`,
  `./scripts/check_lint.sh`, and `./scripts/check_types.sh` for affected typed packages.

## TypeScript TUI

- Preserve the Python event/schema models as the protocol source of truth;
  regenerate client schema and types instead of hand-editing generated files.
- Keep protocol data semantic and rendering/UI state in `clients/tui/`.
  Validate missing, duplicate, out-of-order, reconnect, and terminal events in
  session state transitions.
- Protocol-defined closed sets (run status, event kinds, verdicts) must reach
  the client as generated literal unions, not `string`, and client predicates
  over them must branch exhaustively.
- Respect strict TypeScript settings, including exact optional properties,
  unchecked indexed access, exhaustive control flow, and unused checks. Do not
  use assertions or `any` to bypass uncertain protocol states.
- Check listener/timer/subscription teardown, asynchronous races, stale render
  state, terminal width and Unicode handling, and non-interactive/headless use.
- Run root Biome checks plus `pnpm --dir clients/tui run check` and focused
  Vitest tests when the diff touches the client.

## Go, Rust, C, And C++ Evaluators

- Treat evaluator correctness as adversarial: a candidate must not pass by
  exploiting timing, undefined behavior, weak validation, fixed fixtures, or
  knowledge that would be unavailable in the declared contract.
- For Go concurrency, check goroutine lifetime, channel closure ownership,
  context cancellation, data races, atomic ordering, lock scope, timeouts, and
  deterministic error propagation. Run focused `go test` and `go test -race`
  where feasible.
- For Rust, check ownership and lifetime boundaries, panic/error behavior,
  overflow, `unsafe` invariants, FFI layout, and cleanup. Run `cargo fmt
  --check`, `cargo clippy`, and focused `cargo test` in the owning crate.
- For C/C++, check ABI/layout/alignment, integer overflow, allocation failure,
  pointer lifetime, atomics/memory ordering, partial initialization, and all
  cleanup paths. Use sanitizer or model/conformance tests when available.
- Keep performance measurements separate from correctness gates. Require fixed
  seeds or recorded inputs where reproducibility matters, avoid benchmark
  self-interference, and reject metrics that can improve by skipping work.

## Configuration, Features, And Generated Files

- For TOML/YAML/JSON/manifests, test a representative valid value and unknown,
  malformed, missing, or conflicting values. Check defaults and precedence.
- Add `FeatureFlag` enum members and `FeatureDefinition` registrations together;
  keep typed flag use at call sites and update `docs/contributing/feature-flags.md`.
- Check package and distribution boundaries when adding templates, data, CLI
  entry points, workspace libraries, or client outputs. A source-tree test can
  pass while a built package omits the new file.
- Locate the generator before accepting generated-looking diffs. Reproduce the
  output, inspect the semantic diff, and prevent hand-edited drift.
- For Agent Skills, keep `SKILL.md` frontmatter to `name` and `description`,
  keep router content concise, move depth to direct references, and put VibeSys
  routing rules in `.vibesys.toml` sidecars.

## Prompts, Templates, And Agent Skills

Treat every prompt change as a behavior change that needs an explicit audit:

1. Confirm that prompt assets use the public `vibesys.prompts` rendering API and
   remain in their central loop, domain, or backend owner. Find all render sites
   and compare the fully rendered before/after prompt, not only the Jinja or
   Markdown source diff.
2. Inspect snapshot changes manually. Require updated snapshots when rendered
   output changes; do not accept regeneration as proof that behavior is right.
3. Verify placeholder names, defaults, conditionals, whitespace, role/message
   boundaries, escaping, and behavior for empty, large, or adversarial values.
4. Check instruction priority and scope. Keep neutral skeletons separate from
   domain-specific policy and prevent context intended for one backend, loop,
   modality, or turn from leaking into others.
5. Identify untrusted content and ensure it is clearly delimited and cannot
   silently become authoritative instructions. Do not interpolate secrets or
   unnecessary host state.
6. Audit downstream parsing and tool expectations. Wording changes can alter
   structured output, tool selection, stopping behavior, retries, cost, and
   token usage even when syntax remains valid.
7. Demand a behavioral rationale and targeted evaluation for consequential
   prompt changes. Record what changed in agent-visible output and what evidence
   supports the intended behavior.

For backend prompt fragments, verify the canonical fragment-name registry and
every backend variant change together; an intentional skip still needs an
explicit empty or explanatory fragment. For domain prompts, verify role-file
mapping, optional-role fallback behavior, uniform render variables, and tests
that prevent domain prose from leaking into neutral or unrelated prompts.

For skill changes, also verify that the frontmatter description triggers on the
intended requests without being overly broad, instructions are imperative and
actionable, and references are discoverable directly from `SKILL.md`. For
skills under `resources/skills/`, also run
`uv run pytest tests/entrypoints/test_skills_wiring.py`, which runs
`validate_skill_tree`.

## Documentation And Example Drift

Search for every user-visible name, command, field, default, and path changed by
the PR. Typical coupled documentation includes:

- `README.md`, package READMEs, installation/setup commands, and examples;
- `docs/cli-flags.md` for CLI flags and `docs/contributing/feature-flags.md` for flags;
- `CANDIDATE_CONTRACT.md`, protocol/design docs, and evaluator READMEs;
- example `OBJECTIVE.md`, `vibesys.input.toml`, checker, benchmark, reference,
  requirements, and run commands;
- schema outputs and generated TypeScript protocol types;
- prompt snapshots and skill references.

Flag stale docs when they would cause a user, candidate, maintainer, or agent to
take the wrong action. Do not require prose churn for a purely internal change.

## Tests And Evidence

- Map each stated correctness property to a test, type/schema check, or focused
  manual reproduction. A regression test should fail for the old behavior for
  the reason claimed.
- For a bug fix, require a regression test that fails at the merge base at
  the lowest layer that reproduces the reported symptom. Reject a test that
  passes unchanged on the pre-fix code or that restates the fix's constants
  in the test body. For TUI symptoms stated in terminal geometry, the layer
  is the OpenTUI test renderer (`createTestRenderer`); a pure-function test
  over a formatter is not evidence. See the regression-test table in
  `docs/contributing/tui-architecture.md`.
- Test application configuration and implementation separately, then add a
  focused wiring test when their connection changes.
- Use shared contract tests for interchangeable sandboxes, compute backends,
  and other interface implementations.
- Cover external CLI adapters with success, missing executable, timeout,
  nonzero exit, malformed output, and redaction cases as applicable.
- Prefer the narrowest relevant check first. Broaden when the change crosses an
  architectural, protocol, packaging, or language boundary.
- Treat skipped, flaky, environment-gated, or overly mocked tests as evidence
  gaps, not passes. Separate confirmed findings from risks that require hardware
  or integration environments to verify.
