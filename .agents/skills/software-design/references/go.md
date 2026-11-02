# Go

Go idioms for the design rules, in a module of packages.

## Declaring and enforcing a public interface

- A package's public interface is its exported identifiers. Put everything else
  in unexported names or an `internal` directory, which the compiler keeps
  other modules out of.
- Enforce import direction with a checker over the package graph (such as
  `depguard` or a custom analyzer in the linter), not by convention.

## Size cues

A size limit is a cue to ask rule 5, not a reason to split. Configure function
length, cyclomatic complexity and parameter counts in the linter
(`golangci-lint`), so the numbers live in one place.

## Idioms

- **Types first.** Go has no sum types and no private constructors, so build
  them from what it has. See [types-and-data.md](types-and-data.md).
  - *Valid by construction.* Make a type's fields unexported and give it a
    constructor that validates and returns an error, so code outside the
    package cannot build an invalid value. A proof value is such a type that
    only the checking function returns.
  - *Closed unions.* Model a lifecycle as an interface with an unexported
    method, so only the declaring package implements it, and one struct per
    state. Switch over it with an exhaustiveness linter (`exhaustive` for
    enums, `go-sumtype` for sealed interfaces), because the compiler does not
    check it. Make the zero value invalid or meaningful, since `var x T`
    always compiles.
  - *Identifiers.* A defined type (`type CommitID string`), not an alias and
    not a bare `string`, so the compiler rejects a digest where a commit is
    expected.
- **Closed sets.** A defined integer or string type with named constants, and
  an exhaustive switch.
- **Errors.** Return typed errors that name the offending key or path; wrap
  with `%w` and test with `errors.Is` and `errors.As`.
- **Resources.** `defer` the cleanup in the function that created the
  resource; make `Close` idempotent. Pass a `context.Context` for every
  deadline and cancellation.
- **I/O.** Declare small role-named interfaces where they are consumed, inject
  them into the shell, and ship a fake and a shared contract test with each.
  See [functional-core.md](functional-core.md).
