---
name: software-design
description: Design, structure, or change code in any language. Use when making any code change, especially adding or moving modules, interfaces, dependencies, data flow, config, resource handling, or calls to agents, clusters, or subprocesses, to every bug fix, and to any refactor, split, migration, or contract change.
metadata:
  kind: generic
---

# Software design

The goal is deep modules: a small, stable interface in front of an
implementation that may be as complex as it needs to be. The rest of the system
depends on the interface's guarantees and never thinks about what is behind it.
Loose coupling and clear ownership follow.

These rules are language- and codebase-independent. Read the reference for your
language ([Python](references/python.md), [Go](references/go.md),
[TypeScript](references/typescript.md), [Rust](references/rust.md)); a codebase's
own tools, thresholds and module graph belong in its own skill, so load it too.

## Design checkpoint

Before writing code, answer these in the PR's `Design` section. A change that
adds no type, state or stored data answers 1 and 2 with "no new types or
data"; a new subsystem writes the full sketch first. For 1 and 2 read
[references/types-and-data.md](references/types-and-data.md).

1. **Data.** What are the facts, who owns each, which are stored and which
   derived? How do they flow, how often is each read or written, how much of
   it is there? What does it look like on disk and on the wire? Design the
   data first, then the code that transforms it (rule 15).
2. **Types.** What are the domain types, and which invalid states can they
   not represent? A lifecycle state is a closed union, not a struct of flags
   and optional fields. Which actions need a proof value only a successful
   check can produce? Which raw strings become distinct types (a commit, a
   job ID, a digest)? (rule 16)
3. **Owner.** Which module owns this behavior? If none, is a new one justified?
4. **Interface.** What is the public interface after the change? Is it smaller
   or larger than before?
5. **Direction.** Which way do data and dependencies flow? Does any new import
   point back toward the caller?
6. **Coupling.** What new coupling does this add? Could it be removed instead?
7. **Size.** Does the unit you are growing now need its internals known by its
   callers? See rule 5.
8. **Twice.** Sketch a second, materially different interface. Keep the one
   that hides more.
9. **Mechanism.** For every bug fix, name the mechanism that allowed the bug
   before choosing the fix, search for its other instances (one found is not
   evidence of one), and change the mechanism instead of patching each place.
   Record the class and the instances found, or "none found, searched X".
   See [references/evolving.md](references/evolving.md).
10. **Functional core.** Which part is pure core, which interfaces does the
    shell call, which library owns each, and which implementations exist?
    Where does durable intent live, and how is an interrupted transition
    recovered?

## Rules

1. **Deep modules.** Put complexity inside, not in the interface. Make the
   common case simple, pull complexity downward instead of exposing it as
   options, and prefer computing a default over adding a knob.
2. **Declare the public interface.** Every module has a declared, enforced
   surface with a short contract (what it guarantees, why, how it fails).
   Callers use only that surface. Distinguish published (external callers
   depend on it) from internal (free to change); new modules declare theirs.
3. **An interface promises substitutability.** Share one only if a caller can
   be written once and stay correct for every implementation, failures
   included. Red flags: methods some implementations skip or reject,
   `supports_x` flags, kind checks in callers. If not substitutable, follow
   the fix order in [references/red-flags.md](references/red-flags.md).
4. **One-way dependencies and data flow.** Inputs flow through core into typed
   outputs that consumers interpret. Each layer has its own abstraction; no
   pass-through wrappers. No cycles; prefer removing dependencies to adding.
5. **Factor when callers need internals.** Size is the cue to check, not the
   reason to split. When a unit grows until callers must know how it works,
   make it a unit the dependency tooling can track, with a clear interface.
6. **Policy versus mechanism.** Defaults and selected values live in
   configuration; implementations apply what they are given; wiring connects
   them. A new case changes configuration, not a per-type branch in every
   implementation. Follow the codebase's package layout and placement rule.
7. **Parse at boundaries, typed inside, fail loudly.** Validate external input
   once, at the edge, into typed values; reject unknown keys and name the
   offender. Define an error away only where the semantics are well defined,
   and never mask errors on agent-visible contracts. No fallback may produce a
   plausible-looking result: a terminal status is derived from the work done
   (a run that did nothing did not complete), and a missing required input is
   an error, not a guessed default.
8. **One source of truth.** Store the minimal state and derive the rest.
   Generate downstream definitions from the authoritative one.
9. **Own resources, isolate I/O.** The creator of a resource owns its
   cleanup, on every path, through one construct that every exit passes
   through (return, exception, cancellation, signal, stop request), not a
   handler per exit path. A parent process forwards signals to the owner and
   never kills it before its cleanup runs. Put I/O behind the owning library's
   interface, with lifecycle decisions in the pure core (rule 14).
10. **Distrust external boundaries.** Agents, agent CLIs, clusters, MCP
    clients, and subprocesses delay, fail, and misbehave; design for it at
    the boundary, once. Read [references/boundaries.md](references/boundaries.md)
    when you add or change a call across one, or an agent tool server. In
    short: agent output is untrusted input; an option the run does not offer is
    absent from the schema; every call has a deadline derived from the
    caller's limit; failures are typed; operations are idempotent; retries
    are bounded.
11. **Fit the change to the design.** Make the change as if the design had
    anticipated it, not the smallest diff that works. Prepare first: refactor
    to make the change easy, then make it. Never extend an existing violating
    pattern. Clean only your own path, in a separate commit or PR; otherwise
    file an issue and note it in the `Design` section. Abstract at the third
    case, not the first. Change a contract by expand, migrate, contract. Read
    [references/evolving.md](references/evolving.md) when you refactor, split,
    migrate, or change a contract.
12. **Agent-bound text is a template.** Prompts and the fragments inside them,
    and text agents read later (progress files, tool results), are rendered
    from template files. Code passes data; the template owns the wording,
    conditionals, and loops. Never build prompt text by concatenation or
    formatting, and never append to rendered output. Take
    agent-bound text as a rendered-text type, so each call is a checkable
    sink. A template names reply fields only through the turn's reply schema.
13. **Lint suppressions are explicit opt-outs.** First consider reasonable
    lint-compliant fixes. Suppress only when they would make the design more
    hacky than the current code, and list in the source rationale each
    alternative and why it is worse. Effort, time, and existing violations are
    not reasons by themselves.
14. **Functional core, interfaces and implementations.** Design stateful
    orchestration, scheduling and resource release as `state + event -> new
    state + requests` in a core with no I/O, clock, randomness or asyncio,
    under a thin shell that calls each owning library's interface and feeds
    typed outcomes back as events. Persist intent before I/O and recover
    unfinished transitions. Read [references/functional-core.md](references/functional-core.md)
    for contracts, recovery and naming; every implementation passes the owning
    library's one contract suite.
15. **Data before code.** Design the facts, owners, flows, volumes and
    encodings before the code that transforms them, and choose each
    representation by how it is read, written and sized. A persisted or wire
    format is a contract: one schema (rule 7), changed by expand, migrate,
    contract (rule 11). Unenforced: sketch quality needs judgment; review it.
16. **Types first.** Design the domain types before the functions that use
    them, so invalid states cannot be represented: lifecycles as closed
    unions, proof values for checked actions, a type per identifier. Prefer,
    strongest first, such a type, a lint or dependency-graph rule, then a check
    over the whole codebase; write a rule down only when none is tenable,
    because prose holds until the first author who never read it. A test or
    audit finding that a type could have ruled out means stop and redesign
    ([types-and-data.md](references/types-and-data.md)). Unenforced: choosing
    what becomes a type needs judgment.

## Before handing back

- Re-read the checkpoint answers against the diff; update the `Design` section.
- Run the codebase's boundary and lint checks.
- Apply these rules to code you add or change; do not refactor unrelated code.
