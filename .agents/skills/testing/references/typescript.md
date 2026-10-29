# TypeScript

TypeScript idioms for the testing rules, with `bun:test`.

## Mapping the rules

| Rule | TypeScript |
| --- | --- |
| Public API | A package's exported entry points (its `exports` in `package.json`), and published protocol event data as the frontend contract. Do not import a package's internal files from its tests |
| No patching | Banned: `mock`, `mock.module`, `spyOn`, and assigning over an imported function or object member. Allowed: environment variables and temp directories as inputs |
| Fakes | An in-memory implementation of the same interface, kept beside the interface and shared by consumers |
| Exemption | `// test-isolation: <reason>` on the site's line or the line above |

## Contract suite wiring

Register every Fake and production implementation in the interface's suite.
Run every contract case against each one. Gate whole runs that need real
services behind one opt-in environment variable:

```ts
const realContracts = process.env.REAL_CONTRACTS === "1";

for (const [name, make] of [["fake", makeFake], ["real", makeReal]] as const) {
  describe.skipIf(name === "real" && !realContracts)(`store (${name})`, () => {
    test("missing key raises the documented error", () => {
      expect(() => make().get("absent")).toThrow(KeyMissing);
    });
  });
}
```

## Properties

`fast-check` is the standard property-based library for TypeScript. Without
it, write generator-driven tests with a small seeded generator and pin any
failing input as an explicit example.

## Golden fixtures

Pin a wire protocol with one shared, language-neutral corpus that every
client and server replays, rather than a snapshot per client. For other
public output, keep the snapshot deterministic and review its diff.

## Commands

```bash
bun test path/to/file.test.ts                     # narrowest first
for i in $(seq 20); do bun test path/to/file.test.ts || break; done
```
