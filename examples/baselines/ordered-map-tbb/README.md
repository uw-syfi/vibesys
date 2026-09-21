# oneTBB ordered map baseline

This is the third-party comparison baseline for the `ordered-map` evaluator.
It puts an unmodified `tbb::concurrent_map` behind the VibeSys copying
byte-key ABI, so the scored number is Intel's skip list rather than one written
for this benchmark.

Unlike `queue-spsc-rigtorp`, oneTBB is not vendored. It is a system dependency
(`libtbb-dev` on Debian and Ubuntu, `pkg-config --libs tbb` at link time).
oneTBB is Apache-2.0.

Valid for both scenarios: `swmr` and `mw`.

## What the adapter adds

TBB owns the skip list. Insert, find, and forward iteration are TBB's. The
adapter fills three ABI gaps:

- **No concurrent erase.** `unsafe_erase` is not concurrent with insert or
  find, so the adapter never erases. A remove publishes a null blob on the
  existing node (`std::atomic_store` on the mapped `shared_ptr`). Get/range
  treat a null blob as missing. Tombstone nodes stay until map destruction.
  The benchmark key universe is 256, so the skip list stays bounded by keys
  ever inserted, not by operation count.
- **Unsynchronized mapped values.** TBB does not protect `it->second`. Values
  are immutable strings reached through that atomic `shared_ptr`, so a reader
  can copy a blob while a writer publishes a replacement.
- **Lookup without a heap key.** Keys are `std::string` (SSO covers the 8-byte
  benchmark) with a transparent unsigned-byte comparator, so find/lower_bound
  take `string_view` over the caller's buffer. Range copies from the iterator
  into caller storage; it does not clone items into a temporary vector.

TBB skip-list iterators are forward-only, so `max` and `predecessor` walk from
`begin()`. Those ops are not on the timed mix.

TBB has no per-thread handle. Client objects exist only to satisfy the ABI
lifecycle.

## Why it passes the gate

Point put/get/remove linearize at the skip-list insert or the blob
publish/CAS. Ordered walks are weakly consistent skip-list iteration, which is
the ordered-map contract. Sequential ABI probes and the boundary history see
exact snapshots because they do not overlap mutations.

## Running it

This directory is deliberately outside the `ordered-map-*` input bundles.
VibeSys does not copy it into optimization workspaces, so the optimization
agents cannot inspect or reuse the implementation.

From this directory:

```bash
make
go -C ../../evaluators/ordered-map run . check \
  --workspace ../../baselines/ordered-map-tbb --scenario swmr \
  --operations 24 --trials 100
go -C ../../evaluators/ordered-map run . check \
  --workspace ../../baselines/ordered-map-tbb --scenario mw \
  --operations 24 --trials 100
go -C ../../evaluators/ordered-map run . benchmark \
  --workspace ../../baselines/ordered-map-tbb --scenario swmr \
  --repetitions 3
```
