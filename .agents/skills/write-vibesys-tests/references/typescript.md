# TypeScript test harness

How the `testing` rules map onto the TypeScript clients: `bun:test`, driven
through pnpm workspaces, linted by Biome.

## Mapping the rules

| Rule | Here |
| --- | --- |
| Public API | A package's exported entry points (for example `@vibesys/transport`, `@vibesys/run-state`), and published protocol event data as the frontend contract |
| No patching | No mechanical check yet; reviewers reject `mock`, `mock.module` and `spyOn` |
| Fakes | An in-memory implementation beside the interface, shared by consumers |
| Exemption | `// test-isolation: <reason>` on the site's line or the line above |

## Contract suites

Gate whole runs that need real services with `VIBESYS_REAL_CONTRACTS=1`, the
same switch the Python suite uses.

## Properties

`fast-check` is not yet a dependency. Adding a dependency is a separate
decision: raise it before adding one. Until then, write generator-driven
tests with a small seeded generator and pin any failing input as an explicit
example.

## Wire-protocol conformance

The shared conformance corpus pins framing and connection semantics for every
transport ([wire-protocol.md](../../../../docs/contributing/wire-protocol.md)):

- It holds one fixture per event type, the connection-exchange scenarios, and
  an inventory each runner registers to say which scenarios it executes.
- `pnpm check:conformance` gates coverage, structure and which scenarios each
  runner executes.
- The run-state package folds every event fixture in a test, and a Python test
  replays the scenarios on every server transport.

Extend the corpus for a protocol change instead of adding a per-client
snapshot, and register the scenario in a runner so it executes.

## Commands

```bash
pnpm --filter @vibesys/<package> test              # narrowest first
(cd clients && pnpm lint:ts)                       # Biome
for i in $(seq 20); do pnpm --filter @vibesys/<package> test || break; done
```
