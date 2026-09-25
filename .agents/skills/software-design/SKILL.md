---
name: software-design
description: Design, structure, or change code in any language in this repository. Applies to every code change, especially adding or moving modules, interfaces, dependencies, data flow, config, or resource handling, and to any refactor, split, migration, or contract change.
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
   with exhaustive matching; optional capability interfaces; adapters at the
   wiring layer; duplicating until the third case; extracting only the common
   mechanism. See [references/red-flags.md](references/red-flags.md).
4. **One-way dependencies and data flow.** Inputs flow through core into typed
   outputs that consumers interpret. Each layer has its own abstraction; no
   pass-through wrappers. No cycles. Prefer removing dependencies to adding
   them.
5. **Factor when callers need internals.** Size is the cue to check, not the
   reason to split. When a unit grows until callers must know how it works,
   make it a unit the dependency tooling can track, with a clear interface.
6. **Policy versus mechanism.** Defaults and selected values live in
   configuration; implementations apply what they are given; wiring connects
   them. A new case changes configuration, not a per-type branch in every
   implementation.
7. **Parse at boundaries, typed inside, fail loudly.** Validate external input
   once, at the edge, into typed values; reject unknown keys and name the
   offender. Define an error away only where the semantics are well defined,
   and never mask errors on agent-visible contracts.
8. **One source of truth.** Store the minimal state and derive the rest.
   Generate downstream definitions from the authoritative one.
9. **Own resources, inject effects.** The creator of a resource owns its
   cleanup, on every path. Put side effects (processes, network, clock,
   filesystem) behind a seam so they can be replaced by a Fake; see the
   `testing` skill.
10. **Fit the change to the design.** Make the change as if the design had
    anticipated it, not the smallest diff that works. Prepare first: refactor
    to make the change easy, then make it. Never extend an existing violating
    pattern. Clean only your own path, in a separate commit or PR; otherwise
    file an issue and note it in the `Design` section. Abstract at the third
    case, not the first. Change a contract by expand, migrate, contract, and
    land the contract step. Read
    [references/evolving.md](references/evolving.md) when you refactor, split,
    migrate, or change a contract.

## Before handing back

- Re-read the checkpoint answers against the diff; update the `Design` section.
- Run the language's boundary and lint checks (see its reference).
- Do not refactor unrelated code. Apply these rules to code you add or change.
