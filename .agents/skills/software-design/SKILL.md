---
name: software-design
description: Design, structure, or change code in any language in this repository. Applies to every code change, especially adding or moving modules, interfaces, dependencies, data flow, config, resource handling, or calls to agents, clusters, or subprocesses, to every bug fix, and to any refactor, split, migration, or contract change.
---

# Software design

The goal is deep modules: a small, stable interface in front of an
implementation that may be as complex as it needs to be. The rest of the system
depends on the guarantees of the interface and never has to think about what
is behind it. Loose coupling and clear ownership follow from that.

These rules are language-independent. Tools, thresholds, and idioms live in a
per-language reference; read the one for the language you are editing:

- [references/python.md](references/python.md)
- [references/typescript.md](references/typescript.md)

## Design checkpoint

Before writing code, answer these. Record the answers in the PR's `Design`
section.

1. **Owner.** Which module owns this behavior? If none, is a new one justified?
2. **Interface.** What is the public interface after the change? Is it smaller
   or larger than before?
3. **Direction.** Which way do data and dependencies flow? Does any new import
   point back toward the caller?
4. **Coupling.** What new coupling does this add? Could it be removed instead?
5. **Size.** Does the unit you are growing now need its internals known by its
   callers? See rule 5.
6. **Twice.** Sketch a second, materially different interface. Keep the one
   that hides more.
7. **Mechanism.** For every bug fix, name the mechanism that allowed the bug
   before choosing the fix, then search for its other instances: sibling exit
   paths, roles, backends, tools, and call sites that state the same fact.
   One instance found is not evidence of one instance. Change the mechanism
   instead of patching each place: a constrained type where agent output
   becomes a name or path, one source of truth that consumers project from,
   one owner that every exit path passes through, a failure type set where
   the failure happens. Record the class and the instances found (or "none
   found, searched X") in the PR. If the mechanism fix is too large for the
   PR, land the instance fix only to unblock, and name the mechanism fix as a
   follow-up.
8. **Functional core.** Which part is pure core, which interfaces does the shell
   call for its requests, which library owns each, and which implementations
   exist? Where does durable intent live, and how is an interrupted transition
   recovered?

## Rules

1. **Deep modules.** Put complexity inside, not in the interface. Make the
   common case simple, pull complexity downward instead of exposing it as
   options, and prefer computing a default over adding a knob.
2. **Declare the public interface.** Every module has a declared, enforced
   surface with a short contract (what it guarantees, why, how it fails).
   Callers use only that surface. Distinguish published (external callers
   depend on it) from internal (free to change). New and split modules must
   declare theirs.
3. **An interface promises substitutability.** Share one only if a caller can
   be written once and stay correct for every implementation, failures
   included. Red flags: methods some implementations skip or reject, `supports_x`
   flags, kind checks in callers, a shared contract test that needs skips. If
   not substitutable, prefer, in order: narrow role interfaces; a closed union
   with exhaustive matching; optional capability interfaces; translation at the
   wiring layer; duplicating until the third case; extracting only the common
   mechanism. See [references/red-flags.md](references/red-flags.md).
4. **One-way dependencies and data flow.** Inputs flow through core into typed
   outputs that consumers interpret. Each layer has its own abstraction; no
   pass-through wrappers. No cycles. Prefer removing dependencies to adding
   them.
5. **Factor when callers need internals.** Size is the cue to check, not the
   reason to split. When a unit grows until callers must know how it works,
   make it a unit the dependency tooling can track, with a clear interface.
6. **Policy versus mechanism.** Follow the package layout and placement rule
   in [architecture.md](../../../docs/contributing/architecture.md). Defaults
   and selected values live in
   configuration; implementations apply what they are given; wiring connects
   them. A new case changes configuration, not a per-type branch in every
   implementation.
7. **Parse at boundaries, typed inside, fail loudly.** Validate external input
   once, at the edge, into typed values; reject unknown keys and name the
   offender. Define an error away only where the semantics are well defined,
   and never mask errors on agent-visible contracts. No fallback may produce a
   plausible-looking result: a terminal status is derived from the work done
   (a run that did nothing did not complete), and a missing required input is
   an error, not a guessed default such as a shared temp path.
8. **One source of truth.** Store the minimal state and derive the rest.
   Generate downstream definitions from the authoritative one.
9. **Own resources, isolate I/O.** The creator of a resource owns its
   cleanup, on every path, through one construct that every exit passes
   through (return, exception, cancellation, signal, stop request), not a
   handler per exit path. A parent process forwards signals to the owner and
   never kills it before its cleanup runs. Put I/O (processes, network, clock,
   filesystem) behind the owning library's interface. Keep resource lifecycle
   decisions in the pure core (rule 14); see the `testing` skill.
10. **Distrust external boundaries.** Agents, agent CLIs, clusters, MCP
    clients, and subprocesses delay, fail, and misbehave; design for it at
    the boundary, once. Read
    [references/boundaries.md](references/boundaries.md) when you add or
    change a call across one, or an agent tool server (its own module,
    library, or standalone server under `resources/`). In short: agent output is untrusted input, and
    an option the run does not offer is absent from the schema, not rejected
    after the fact; every call has a deadline derived from the caller's
    limit; failures are typed (transient, permanent, unsupported);
    operations are idempotent so retry and cancel are safe; transport retries
    are bounded and only for transient failures of idempotent operations; what
    the system offers is derived from what the executor reports it supports.
11. **Fit the change to the design.** Make the change as if the design had
    anticipated it, not the smallest diff that works. Prepare first: refactor
    to make the change easy, then make it. Never extend an existing violating
    pattern. Clean only your own path, in a separate commit or PR; otherwise
    file an issue and note it in the `Design` section. Abstract at the third
    case, not the first. Change a contract by expand, migrate, contract, and
    land the contract step. Read
    [references/evolving.md](references/evolving.md) when you refactor, split,
    migrate, or change a contract.
12. **Agent-bound text is a template.** Prompts, system prompts, and the
    fragments inside them are rendered by `vs_prompts` from `.j2` files.
    Python passes data; the template owns the wording, conditionals, and loops.
    Never build prompt text with `+`, f-strings, `.format`, or `.join`, and
    never append to rendered output. `tests/architecture/test_prompt_templates.py`
    enforces this. Text agents read later (progress files, tool results) is
    agent-bound too: take it as a `RenderedPrompt` parameter, which makes the
    call a checked sink. A prompt that points the agent at written text takes
    the proof of the write as data (a `ProgressEntry` from `ProgressLog.append`)
    and guards the pointer on it; `tests/architecture/test_progress_pointers.py`
    enforces this.
13. **Lint suppressions are explicit opt-outs.** First consider reasonable
    lint-compliant fixes. Suppress only when those fixes would make the design
    more hacky than retaining the current code. In the source rationale, list
    the alternatives considered and explain why each is worse. Effort, time,
    and existing violations are not reasons by themselves.
14. **Functional core, interfaces and implementations.** Design stateful
    orchestration, workstream and hypothesis lifecycle, scheduling, evaluation
    lifecycle, and resource release as `state + event -> new state + requests`.
    Keep I/O, clocks, randomness, and asyncio out of the core. Let a thin shell
    call each owning library's interface and return typed outcomes as events.
    Persist intent before I/O and recover unfinished transitions. This keeps
    decisions modular and makes exhaustive stress testing practical. Read
    [references/functional-core.md](references/functional-core.md) for contracts,
    placement, recovery, and naming.
    In prose, say "interface" and "implementation"; an interface is a
    `typing.Protocol` in its owning library's `.api`, named by role with no
    suffix (`Cluster`, `StateStore`, `AgentSessions`). Implementations are
    `<Variant><Role>` (`SlurmCluster`, `FakeCluster`, `DockerSandbox`,
    `ModalSandbox`); each interface has several substitutable implementations
    that all pass one contract test suite shipped by the owning library.

## Before handing back

- Re-read the checkpoint answers against the diff; update the `Design` section.
- Run the language's boundary and lint checks (see its reference).
- Do not refactor unrelated code. Apply these rules to code you add or change.
