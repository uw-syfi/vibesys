---
name: testing
description: Write, change, review, or fix tests in any language in this repository. Use when adding or editing any test, fixing a bug (regression test), tempted to patch or mock, adding or changing a Fake, writing property-based or golden-fixture tests, or diagnosing a flaky test.
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
   code is missing a seam: add an injectable port (constructor argument or
   parameter) and pass a Fake.
3. **Integrate modules with Fakes.** A Fake is an in-memory, faithful
   implementation of the same API, not a stub with canned answers. Whoever
   changes the real behavior owns the Fake and its contract test in the same
   PR. See [references/fakes-and-contracts.md](references/fakes-and-contracts.md).
4. **Test properties, not instances.** Default to property-based tests and
   fuzzing for parsers, serializers, validators, pure logic, and state
   machines. Use single examples only for a named scenario or a regression.
   Use golden fixtures where behavior reduces to a deterministic state
   snapshot. See
   [references/properties-and-goldens.md](references/properties-and-goldens.md).
5. **No flaky tests.** Never depend on timeouts, sleeps, wall-clock time,
   scheduling order, or shared state. Inject clocks and simulate timeouts; do
   not wait for them. Never add retries. See
   [references/flakiness.md](references/flakiness.md).
6. **Bug fixes.** Add a regression test at the lowest layer that reproduces the
   symptom, through the public API, that fails at the merge base. Then add a
   property test that generalizes the pattern, so the whole class of bug is
   covered and not just the reported instance.

## Before handing back

- Run the narrowest test target first, then broaden (commands per language).
- Run the language's test-isolation check if one exists (see its reference).
  It is a shrink-only ratchet: existing violations are baselined, new ones
  fail, and removing sites from a baselined file means lowering its recorded
  counts.
- Do not migrate unrelated tests to these rules. Apply them to new tests and to
  the tests you are already changing.
- A test involving threads, processes, async work, or time must pass 20
  consecutive runs and a parallel run before you hand it back.
