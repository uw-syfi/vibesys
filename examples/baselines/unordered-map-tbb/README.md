# oneTBB unordered map baseline

This is the third-party comparison baseline for the `unordered-map` evaluator.
It puts an unmodified `tbb::concurrent_hash_map` behind the VibeSys copying
byte-key ABI, so the scored number is Intel's structure rather than one written
for this benchmark.

Unlike `queue-spsc-rigtorp`, oneTBB is not vendored. It is a system dependency
(`libtbb-dev` on Debian and Ubuntu, `pkg-config --libs tbb` at link time).
oneTBB is Apache-2.0.

Valid for both scenarios: `swmr` and `mw`.

## What the adapter adds

TBB owns the map. The adapter copies keys and values into `std::vector`
buffers, hashes those bytes with FNV-1a, and holds a TBB accessor for the
duration of each call. Get and remove therefore peek before committing an
undersized `VSUM_INVALID`, so the mapping stays put when the output does not
fit. There is no item-capacity argument; the map is unbounded.

TBB has no per-thread handle. Client objects exist only to satisfy the ABI
lifecycle.

## Why it is linearizable

`tbb::concurrent_hash_map` serializes operations on one key through a
per-bucket accessor. Put, get, and remove of the same key take effect under
that accessor, so each call has a single linearization point. Operations on
different keys commute, which is the unordered-map spec.

## Running it

This directory is deliberately outside the `unordered-map-*` input bundles.
VibeSys does not copy it into optimization workspaces, so the optimization
agents cannot inspect or reuse the implementation.

From this directory:

```bash
make
go -C ../../evaluators/unordered-map run . check \
  --workspace ../../baselines/unordered-map-tbb --scenario swmr \
  --operations 24 --trials 100
go -C ../../evaluators/unordered-map run . check \
  --workspace ../../baselines/unordered-map-tbb --scenario mw \
  --operations 24 --trials 100
go -C ../../evaluators/unordered-map run . benchmark \
  --workspace ../../baselines/unordered-map-tbb --scenario mw \
  --repetitions 3
```
