# TypeScript test harness

How the `testing` rules map onto the TypeScript clients: `bun:test`, driven
through pnpm workspaces, linted by Biome.

## Mapping the rules

| Rule | Here |
| --- | --- |
| Public API | A package's exported entry points (for example `@vibesys/display-names`, `@vibesys/ui`), and published protocol event data as the frontend contract |
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

Shared telemetry, subscription-frame and run-conversion fixtures pin the inputs
consumed by the Go gateway and Python readers. Extend the relevant fixture and
consumer tests for a contract change. `test_gateway_conversion_fixture_regenerates_identically`
checks that the retained conversion fixture still comes from the native writer.

## Commands

```bash
pnpm --filter @vibesys/<package> test              # narrowest first
(cd clients && pnpm lint:ts)                       # Biome
for i in $(seq 20); do pnpm --filter @vibesys/<package> test || break; done
```
