# TypeScript

Runner: `bun:test`, driven through pnpm workspaces under `clients/`. Lint: Biome.

## Mapping the rules

| Rule | TypeScript |
| --- | --- |
| Public API | A package's exported entry point (`clients/backend-client`, `clients/core-state`), and published protocol event data as the frontend contract. Do not import a package's internal files from its tests |
| No patching | Banned: `mock`, `mock.module`, `spyOn`, and assigning over an imported function or object member. Allowed: environment variables and temp directories as inputs |
| Fakes | An in-memory implementation of the same interface, kept beside the interface and shared by consumers |
| Exemption | `// test-isolation: <reason>` on the site's line or the line above |
| Geometry symptoms in the TUI | Reproduce through the OpenTUI test renderer (`createTestRenderer` from `@opentui/core/testing`), not a formatter unit test |

## Contract suite wiring

Run the same suite over the Fake and the real implementation. Skip the real one
unless `VIBESYS_REAL_CONTRACTS=1`:

```ts
const realContracts = process.env.VIBESYS_REAL_CONTRACTS === "1";

for (const [name, make] of [["fake", makeFake], ["real", makeReal]] as const) {
  describe.skipIf(name === "real" && !realContracts)(`store (${name})`, () => {
    test("missing key raises the documented error", () => {
      expect(() => make().get("absent")).toThrow(KeyMissing);
    });
  });
}
```

## Properties

`fast-check` is the standard property-based library for TypeScript, but it is
not yet a dependency of this repo. Adding a dependency is a separate decision:
raise it before adding one. Until then, write generator-driven tests with a
small seeded generator and pin any failing input as an explicit example.

## Golden fixtures

Wire-protocol behavior is pinned by the shared conformance corpus in
`tests/conformance/` (see `docs/contributing/wire-protocol.md`). Extend the
corpus for protocol changes rather than adding a per-client snapshot. For other
public output, keep the snapshot deterministic and review its diff.

## Commands

```bash
pnpm --filter @vibesys/<package> test              # narrowest first
(cd clients && pnpm lint:ts)                       # Biome
for i in $(seq 20); do pnpm --filter @vibesys/<package> test || break; done
```

There is no mechanical test-isolation check for TypeScript yet; apply the rules
by review.
