---
name: testing
description: Write, change, review, or fix tests in any language in this repository. Use when adding or editing any test, fixing a bug (regression test), tempted to patch or mock, adding or changing a Fake, writing property-based, golden-fixture, or fault-injection tests, testing code that calls agents, clusters, or subprocesses, or diagnosing a flaky test.
---

# Testing

Write high-value tests: tests that keep passing through refactors and fail only
when observable behavior changes. A test is low value if renaming a private
function, splitting a module, or swapping an implementation detail breaks it.

These rules are language-independent. Tool names, commands, and syntax live in
a per-language reference; read the one for the language you are editing:

- [references/python.md](references/python.md)
- [references/typescript.md](references/typescript.md)
- Another language: apply the same rules with that language's idioms, and add a
  reference file for it in the same shape.

## Rules

1. **Test the public API only.** The public API is the surface other packages
   or modules are already allowed to depend on: a library's `api` entry point,
   the module API that sibling modules use inside a package, and the
   application's facade or serialized events at the top. Do not import private
   modules or assert on private state. The one exception is a test whose point
   is a tricky implementation detail; mark that site
   `test-isolation: <specific reason>` in a comment.
2. **No patching or mocking.** Do not replace code at runtime (monkeypatching
   attributes, module mocking, spies, mock frameworks). It couples the test to
   how the code is written. Setting inputs is fine: environment variables,
   working directory, temp directories. If a test seems to need a patch, the
   code is missing a seam: inject the owning library's interface through a
   constructor argument or parameter, and pass a Fake implementation.
3. **Integrate modules with Fakes.** A Fake is an in-memory, faithful
   implementation of the same API, not a stub with canned answers. A Fake is
   never more capable or more forgiving than production: it reports the same
   supported capabilities (derived from the same definition), validates and
   loads state as strictly, and enforces the same limits. Whoever changes the
   real behavior updates the Fake in the same PR. The owning library ships one
   contract suite per interface; run it against every implementation, Fake and
   production, without skipping contract cases. See
   [references/fakes-and-contracts.md](references/fakes-and-contracts.md).
4. **Test properties, not instances.** Default to property-based tests and
   fuzzing for parsers, serializers, validators, pure logic, and state
   machines. Use single examples only for a named scenario or a regression.
   Use golden fixtures where behavior reduces to a deterministic state
   snapshot. Test functional cores with pure properties over generated event
   sequences, including crash and replay at every durable intent boundary. Use
   no async, sleeps, or Fakes in core tests. See
   [references/properties-and-goldens.md](references/properties-and-goldens.md).
5. **No flaky tests.** Never depend on timeouts, sleeps, wall-clock time,
   scheduling order, or shared state. Inject clocks and simulate timeouts; do
   not wait for them. Never add retries. See
   [references/flakiness.md](references/flakiness.md).
6. **Bug fixes.** Add a regression test at the lowest layer that reproduces the
   symptom, through the public API, that fails at the merge base. Then add a
   property test that generalizes the pattern, so the whole class of bug is
   covered and not just the reported instance. Example: for an uppercase
   hypothesis id that broke workspace names, test every agent-supplied id with
   a property test, not only the uppercase one. The class is the mechanism
   named under the `software-design` skill's Mechanism checkpoint; a test that
   replays the one reply or exit path that triggered the bug does not cover
   it.
7. **Test the unhappy paths at boundaries.** Canned replies that match what
   the code expects test only the happy path. Where code meets an agent, a
   cluster, an MCP client, or a subprocess, drive it with generated behavior:
   agent output generated from the declared schema, including options the run
   does not offer, repeated mistakes, malformed output, and replies slower
   than the caller's deadline; and scheduled faults on the far side. For a
   scope that owns a resource, test as a property that an exit at any point
   (each step, exception, cancellation, signal, stop) releases the resource
   and ends in a typed terminal state. Composed-system chaos tests inject
   implementation failures and unknown exceptions; require a typed terminal
   state, never an implicit success. Test an agent tool server with synthesized
   tool calls and call sequences, not agents. Assert invariants, not one
   expected trace. See
   [references/fault-injection.md](references/fault-injection.md).
8. **Test process and signal code in process.** Logic that runs in a child
   process or a signal handler is invisible to coverage and slow to test
   through subprocesses. Put it behind a function the test calls directly
   with injected I/O interfaces; keep at most one subprocess test per entry
   point to prove the wiring.

## Before handing back

- Run the narrowest test target first, then broaden (commands per language).
- Run the language's test-isolation check if one exists (see its reference).
  It is a shrink-only ratchet: existing violations are baselined, new ones
  fail, and removing sites from a baselined file means lowering its recorded
  counts.
- Do not migrate unrelated tests to these rules. Apply them to new tests and to
  the tests you are already changing.
