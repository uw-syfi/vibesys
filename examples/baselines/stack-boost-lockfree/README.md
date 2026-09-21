# Boost.Lockfree stack baseline

This is the third-party comparison baseline for the `stack` evaluator. It puts
an unmodified `boost::lockfree::stack` behind the VibeSys copying byte-value
ABI, so the scored number is Boost's Treiber stack rather than one written for
this benchmark.

Boost.Lockfree is header-only (`libboost-dev` on Debian and Ubuntu). Boost is
BSL-1.0.

Valid for all four scenarios: `spsc`, `mpsc`, `spmc`, and `mpmc`.

## What the adapter adds

Boost owns LIFO ordering. The adapter supplies the two things the ABI needs
and `boost::lockfree::stack` does not have:

- **Bounding.** The Boost container is a node-pool Treiber stack. A second
  Boost stack of free slot indices is the capacity token: a producer takes a
  slot before it copies and a consumer returns a slot after it copies, so
  `VSST_FULL` is reported only when all `capacity` slots are held and neither
  `memcpy` runs inside a Boost operation. Both stacks are constructed with
  `capacity` nodes and use `bounded_push`, so a successful push does not
  allocate.
- **Value storage.** Stack entries are slot indices into a fixed-stride arena,
  so payload bytes never move while Boost splices nodes. The arena is reserved
  at stack creation.

Boost has no per-thread handle. Producer and consumer objects exist only to
satisfy the ABI lifecycle.

## Why it is linearizable

`boost::lockfree::stack` is a Treiber stack: push and pop CAS on the head, so
each successful operation has a single linearization point and the order is
LIFO. Slot indices are unique occupancy tokens. Duplicate payloads occupy
distinct slots.

Taking a slot before publishing, and holding one while copying a popped value
out, is a capacity reservation. The contract permits this explicitly for
`mpmc`. For the strictly linearizable scenarios both windows sit inside the
enclosing call, and the windows scale with `--value-size`.

## Limitations

- **`VSST_INVALID` is not externally atomic.** Boost cannot peek, so an
  undersized output is only detectable after the item has been removed. The
  adapter requeues the untouched slot and returns `VSST_INVALID`, which leaves
  a window where the item is invisible to other consumers. The contract asks
  for the item to stay stacked. This passes today only because the evaluator
  issues undersized pops from the sequential ABI probe, never from the
  concurrent history, so the window is unobservable.

## Running it

This directory is deliberately outside the `stack-*` input bundles. VibeSys
does not copy it into optimization workspaces, so the optimization agents
cannot inspect or reuse the implementation.

From this directory:

```bash
make
go -C ../../evaluators/stack run . check \
  --workspace ../../baselines/stack-boost-lockfree --scenario spsc \
  --operations 24 --trials 100
go -C ../../evaluators/stack run . check \
  --workspace ../../baselines/stack-boost-lockfree --scenario mpsc \
  --operations 24 --trials 100
go -C ../../evaluators/stack run . check \
  --workspace ../../baselines/stack-boost-lockfree --scenario spmc \
  --operations 24 --trials 100
go -C ../../evaluators/stack run . check \
  --workspace ../../baselines/stack-boost-lockfree --scenario mpmc \
  --operations 24 --trials 100
go -C ../../evaluators/stack run . benchmark \
  --workspace ../../baselines/stack-boost-lockfree --scenario mpmc \
  --repetitions 3
```
