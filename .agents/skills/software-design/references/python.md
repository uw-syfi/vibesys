# Python

Python idioms for the design rules.

## Declaring and enforcing a public interface

- Give each package one public module (an `api` module or the package
  `__init__`) and keep everything else underscore-private or unexported.
- Python cannot hide a module, so enforce the surface with a module-boundary
  tool (such as tach or import-linter) that declares each module's allowed
  dependencies and exposed interface and fails on an undeclared edge or a
  cycle.
- Declare a new or split module's interface the same way existing ones are
  declared; a module that lacks a declaration is not license for the next.
- Add a dependency edge in the same change as the import, and prefer
  removing one to adding one.

## Size cues

A size limit is a cue to ask rule 5, not a reason to split. Configure file
length, function complexity (`C901`), branches, statements and arguments
(Ruff's `PLR` rules) in the linter, so the numbers live in one place.

## Idioms

- **Boundary types.** Pydantic models for external contracts, with
  `model_config = ConfigDict(extra="forbid")` so unknown keys are rejected.
  A model that must accept unknown keys sets `extra` explicitly and says why.
- **Closed sets.** `StrEnum` or `Literal`, never raw strings. Look up
  implementations in a registry keyed by the enum. Match exhaustively.
- **Types first.** Model a lifecycle as a union of frozen dataclasses (or
  models), one per state, not a class of flags and optionals; give identifiers
  `NewType` or a frozen wrapper. See [types-and-data.md](types-and-data.md).
- **Errors.** Typed errors that name the offending key or path.
- **Proof types.** Parse an external response once into a frozen type that
  only the parser constructs, and make guards take that type, not raw fields
  the caller supplies. Outcome unions end in `assert_never`, so the type
  checker flags an unhandled variant.
- **Resources.** Context managers or an explicit `close()`; cleanup must be
  idempotent.
- **I/O.** Declare role-named `typing.Protocol` interfaces in the owning
  library's public module. Inject them into the shell, never the pure core.
  Ship a Fake and shared contract suite with each interface. See
  [functional-core.md](functional-core.md).
- **Generated contracts.** Generate downstream types (JSON Schema,
  TypeScript) from the authoritative Pydantic models; never hand-copy them.
