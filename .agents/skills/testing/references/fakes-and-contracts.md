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

## Keeping a Fake faithful

The developer who changes real behavior updates the Fake in the same PR. Two
mechanisms make drift visible.

**Contract suite.** One test suite, run against both the Fake and the real
implementation, asserts the API's semantics (errors, ordering, idempotence,
state transitions). The Fake run is part of the normal test run. The real run
is opt-in: it is skipped unless `VIBESYS_REAL_CONTRACTS=1`, and it is triggered
manually for now, not by CI. Run it whenever you change the real implementation
or its Fake, and say so in the PR's Verification section. The per-language
reference shows how to wire the opt-in.

**Differential test.** For stateful APIs, drive the Fake and the real
implementation with the same random sequence of operations and assert their
observable results agree. Gate the real side with the same opt-in.

## When you change real behavior

1. Change the real implementation.
2. Change the Fake to match, or extend the Fake if the API grew.
3. Add or update the contract test that pins the new semantics.
4. Run the real-implementation contract suite for that API manually and note
   the result.
