# Verus Ordered Map Candidate

This isolated subcrate explores a verifier-gated ordered map. It exposes a
pure-Rust `OrderedMap` of `u64` keys to `u64` values through fixed `new`,
`put`, `get`, `remove`, `min`, `max`, `predecessor`, `successor`, `range`, and
`len` operations.

The task owns every file outside `src/candidate/**`. `MapToken` is the
client-owned view of the abstract strictly increasing unique-key sequence. The
fixed facade gives each operation an `AtomicUpdate` whose postcondition defines
exact linearizable point ops and ordered snapshots.

The Verus standard-library dependency is pinned to the release matching
`Verus 0.2026.08.30.b432e82`.

```bash
cargo check --manifest-path verus-ordered-map/Cargo.toml
cargo test --manifest-path verus-ordered-map/Cargo.toml
cargo verus verify --manifest-path verus-ordered-map/Cargo.toml
```
