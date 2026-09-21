# Verus MPMC LIFO Candidate

This isolated subcrate explores a verifier-gated stack candidate. It exposes a
bounded pure-Rust `MpmcStack<T>` through fixed `new`, `push`, `pop`, and `len`
operations.

The task owns every file outside `src/candidate/**`, including the manifest,
lockfile, module wiring, contract, facade, README, and ignore rules.
`LifoToken<T>` is the client-owned view of the abstract `Seq<T>` history, with
the top at the end. The fixed facade gives each operation an `AtomicUpdate`
whose postcondition defines exact bounded LIFO behavior, then delegates that
obligation to the candidate.

This is a safety and strict-LIFO prototype. It does not prove lock acquisition
termination, starvation freedom, or weak-memory properties beyond those
supplied by Verus's sequentially consistent atomic library.

The candidate owns its representation, synchronization, invariants, operation
bodies, and the step that resolves each logical update. It may also transfer an
update through a candidate invariant for helping. The fixed facade contains no
runtime synchronization and does not choose a physical linearization point.

Unlike the native stack evaluator, this task does not permit capacity
reservation before publication.

The Verus standard-library dependency is pinned to the release matching
`Verus 0.2026.08.30.b432e82`.

```bash
cargo check --manifest-path verus-mpmc/Cargo.toml
cargo test --manifest-path verus-mpmc/Cargo.toml
cargo verus verify --manifest-path verus-mpmc/Cargo.toml
```
