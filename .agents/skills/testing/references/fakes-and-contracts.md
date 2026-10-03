# Fakes and contract tests

## What a Fake is

A Fake implements the same API as the real component, entirely in memory, with
the same semantics: same errors, same ordering, same idempotence, same state
transitions. It exists so a test can integrate several modules without a
database, subprocess, sandbox, or network.

A stub that returns canned values, or a mock that records calls, is not a
Fake. It encodes one caller's expectations, not the component's behavior.

## Where Fakes live

- Next to the API they fake, in the same package, so every consumer reuses one.
  Export it through the package's public entry point when consumers outside the
  package need it.
- Not copied per test file. When a second test module needs a Fake that is
  defined inside a test file, move it beside the API it fakes.
- A Fake must not pull in the real implementation's heavy dependencies.

## Failure knobs

Give every Fake explicit, deterministic ways to fail: a timeout, a nonzero
exit, malformed output, a missing tool, a permission error. The test asks for
the failure; the Fake raises it immediately. Never make a test wait for a real
timeout, and never patch a filesystem or process call to force a failure.

## Never more capable or forgiving than production

A Fake that accepts what production rejects hides bugs until a live run. Pin
the facts that differ most often:

- Capabilities: derive what the Fake supports from the same definition the
  production executor uses, not from a list in the Fake.
- Validation and loading: parse and load state with the production models and
  the same strictness (unknown keys rejected).
- Limits: enforce the same deadlines, sizes, and path rules, measured on the
  injected clock.

## Keeping a Fake faithful

The developer who changes real behavior updates the Fake in the same PR. Two
mechanisms make drift visible.

**Contract suite.** The library that owns an interface ships one suite for it.
Register every implementation, Fake and production, in that suite. Run all
contract cases against each one. Assert strict validation, typed errors and
outcomes (including Unknown), ordering, cancellation, idempotence, state
transitions, and recovery after lost acknowledgements. No
implementation-specific skips may hide a contract mismatch; narrow the
interface when implementations are not substitutable. See the
[functional core rule](../../software-design/references/functional-core.md).

The Fake run is part of the normal test run. Production runs requiring real
services are opt-in with `VIBESYS_REAL_CONTRACTS=1`, triggered manually for now,
not by CI. This gates the whole run, not individual contract cases. Run the
suite for every affected implementation when you change the interface, a
production implementation, or its Fake. Report the results and any unavailable
environments in the PR's Verification section. The per-language reference
shows how to wire the opt-in.

**Differential test.** For stateful APIs, drive the Fake and the real
implementation with the same random sequence of operations and assert their
observable results agree. Gate the real side with the same opt-in.

## When you change real behavior

1. Change the real implementation.
2. Change the Fake to match, or extend the Fake if the API grew.
3. Add or update the contract test that pins the new semantics.
4. Run the interface's suite against every affected implementation and note
   the results, including production runs.
