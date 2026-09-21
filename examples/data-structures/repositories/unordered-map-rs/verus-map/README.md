# Verus Unordered Map Candidate

This isolated subcrate explores a verifier-gated unordered map. It exposes a
pure-Rust `ConcurrentMap` of `u64` keys to `u64` values through fixed `new`,
`put`, `get`, `remove`, and `len` operations.

The task owns every file outside `src/candidate/**`, including the manifest,
lockfile, module wiring, contract, facade, README, and ignore rules.
`MapToken` is the client-owned view of the abstract unique-key sequence. The
fixed facade gives each operation an `AtomicUpdate` whose postcondition defines
exact linearizable put/get/remove, then delegates that obligation to the
candidate.

There is no ordering, range, or iteration contract. Operations on different
keys commute.

The Verus standard-library dependency is pinned to the release matching
`Verus 0.2026.08.30.b432e82`.

```bash
cargo check --manifest-path verus-map/Cargo.toml
cargo test --manifest-path verus-map/Cargo.toml
cargo verus verify --manifest-path verus-map/Cargo.toml
```
