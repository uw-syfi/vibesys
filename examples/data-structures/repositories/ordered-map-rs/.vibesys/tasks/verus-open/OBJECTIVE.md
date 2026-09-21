Optimize a formally verified, pure-Rust concurrent ordered map of `u64` keys
to `u64` values.

Headline metric: `total_ops_per_sec` (maximize).

The candidate is the `ordered-map-verus` library crate at `verus-ordered-map/`.
Preserve this fixed public interface:

```rust
pub struct OrderedMap;

impl OrderedMap {
    pub fn new() -> Self;
    pub fn put(&self, key: u64, value: u64) -> Option<u64>;
    pub fn get(&self, key: u64) -> Option<u64>;
    pub fn remove(&self, key: u64) -> Option<u64>;
    pub fn min(&self) -> Option<(u64, u64)>;
    pub fn max(&self) -> Option<(u64, u64)>;
    pub fn predecessor(&self, key: u64) -> Option<(u64, u64)>;
    pub fn successor(&self, key: u64) -> Option<(u64, u64)>;
    pub fn range(&self, start: u64, end: u64, max_items: usize) -> (Vec<(u64, u64)>, bool);
    pub fn len(&self) -> usize;
}
```

Internally, `MapToken` and each operation's `AtomicUpdate` obligation are
proof-only. They connect the task-owned abstract unique-key map to the
candidate's concrete state and erase from the optimized executable.

The map has exact linearizable point and ordered-operation semantics. Every
completed operation takes effect at one point between its invocation and return:

- `new()` creates an empty map. The map is unbounded.
- `put(key, value)` inserts or replaces. It returns `None` when `key` was
  missing and `Some(old)` when it replaced `old`. Inserted keys stay strictly
  increasing.
- `get(key)` returns the current value, or `None` when the key is missing, and
  does not mutate the map.
- `remove(key)` returns and deletes the current value, or `None` when missing.
- `min` / `max` return the least / greatest mapping, or `None` on empty.
- `predecessor(key)` returns the greatest key strictly below `key`.
- `successor(key)` returns the least key strictly above `key`.
- `range(start, end, max_items)` is a snapshot of keys in `[start, end)` in
  increasing order, truncated to `max_items`, plus a remaining flag.
- `len()` returns the number of keys at its linearization point.

Unlike the native ordered-map evaluator, overlapping ordered operations are
still exact snapshots, not weakly consistent iteration. Values are never lost,
duplicated, or fabricated.

Only files below `verus-ordered-map/src/candidate/` are implementer-owned. Keep the
fixed manifest, module wiring, contract, and public facade unchanged. The
accuracy command checks those files, runs both a real Rust compilation and
`cargo verus verify`, then exercises the public Rust API from task-owned code.
The candidate owns the representation, synchronization primitives, invariants,
operation bodies, and the physical points at which it resolves the fixed
logical updates. An implementation may transfer an update through a candidate
invariant to support helping. The fixed facade delegates to the candidate and
does not contain a lock or select a linearization strategy. Verification must
finish with zero errors. Do not use an inconsistent executable path under
ordinary Cargo and verification, or introduce unsound assumptions merely to
make the verifier accept the candidate.

This open-track task evaluates safety and functional refinement. It does not
claim a formal proof of lock-freedom, wait-freedom, starvation freedom, allocator
progress, scheduler fairness, or the Rust/LLVM toolchain.
