# Rust

Rust idioms for the design rules, in a workspace of crates.

## Declaring and enforcing a public interface

- A crate's public interface is what its crate root exposes. Keep modules private and
  re-export the surface with `pub use`; use `pub(crate)` for anything shared
  inside the crate. Turn on the `unreachable_pub` lint to catch `pub` items the
  crate does not export.
- Crate dependencies are the enforced graph: Cargo rejects cycles between
  crates. Split a crate when its modules need one-way dependencies held by the
  compiler, and check the allowed edges with `cargo deny` or a workspace test.

## Size cues

A size limit is a cue to ask rule 5, not a reason to split. Configure function
length, cognitive complexity and argument counts in `clippy.toml`, so the
numbers live in one place.

## Idioms

- **Types first.** The language has the pieces, so use them. See
  [types-and-data.md](types-and-data.md).
  - *Valid by construction.* Keep fields private and return `Result` from the
    constructor, or implement `TryFrom`, so code outside the module cannot
    build an invalid value. A proof value is such a type with no public
    constructor, returned only by the checking function.
  - *Closed unions.* Model a lifecycle as an `enum` whose variants carry only
    the data that state has, and `match` without a `_` arm, so a new variant
    fails to compile at every site. Mark `#[non_exhaustive]` only on a
    published enum that must grow.
  - *Identifiers.* A newtype (`struct CommitId(String)`), not a `type` alias
    and not a bare `String`, so the compiler rejects a digest where a commit
    is expected.
  - *Consuming transitions.* Take `self` by value when a value must not be
    used again, so a used token or a finished state cannot be reused.
- **Closed sets.** A fieldless `enum`, with `serde` `deny_unknown_fields` on the
  structs that parse external input.
- **Errors.** Return a typed `enum` error that names the offending key or path;
  `?` to propagate. Reserve `unwrap` and `panic!` for broken invariants, and
  deny `clippy::unwrap_used` outside tests.
- **Resources.** `Drop` ties cleanup to ownership on every exit path, but it
  cannot return an error or `await`, so give a resource with a failing or async
  cleanup an explicit consuming `close`. Cancel through a token passed down,
  remembering a dropped future stops at its last `await`.
- **I/O.** Declare small role-named traits, inject them into the shell, and ship
  a fake and a shared contract test with each. Prefer generics, and use `dyn`
  where the implementation is chosen at run time. See
  [functional-core.md](functional-core.md).
- **`unsafe`.** Confine it to one small module with a safe interface, and write
  the invariant it relies on in a `// SAFETY:` comment at each block.
