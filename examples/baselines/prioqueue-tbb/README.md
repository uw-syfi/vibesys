# oneTBB priority queue baseline

This is the third-party comparison baseline for the `priority-queue` evaluator.
It puts an unmodified `tbb::concurrent_priority_queue` behind the VibeSys
copying byte-value ABI, so the scored number is Intel's structure rather than
one written for this benchmark.

Unlike `queue-spsc-rigtorp`, oneTBB is not vendored. It is a system dependency
(`libtbb-dev` on Debian and Ubuntu, `pkg-config --libs tbb` at link time),
validated here against oneTBB 2021.5. oneTBB is Apache-2.0.

Valid for all four scenarios: `spsc`, `mpsc`, `spmc`, and `mpmc`.

## What the adapter adds

TBB owns the ordering. The adapter supplies the two things the ABI needs and
`tbb::concurrent_priority_queue` does not have:

- **Bounding.** The TBB container is unbounded. A lock-free stack of payload
  slots is the capacity token: a producer takes a slot before it copies and a
  consumer returns a slot after it copies, so `VSPQ_FULL` is reported only when
  all `capacity` slots are held and neither `memcpy` runs inside a TBB
  operation. The stack packs a 32-bit ABA tag beside the slot index and bumps it
  on every push and pop, so a successful CAS observes an unchanged stack and
  `acquire` fails only on genuine emptiness. That is what makes `VSPQ_FULL`
  exact rather than conservative.
- **Value storage.** Heap entries are `(priority, slot)` pairs into a
  fixed-stride arena, so payload bytes never move while TBB sifts. Both the
  arena and the TBB heap vector are reserved at queue creation, so the bridge
  adds no per-operation allocation.

`try_pop` removes the *greatest* element under `Compare`, and the contract wants
the lowest numeric priority, so the comparator is inverted rather than the
priorities being rewritten.

## Why it is linearizable

`tbb::concurrent_priority_queue` is flat-combining: each operation is published
as a node on a lock-free list, one thread wins the role of combiner, and that
thread executes the whole pending batch alone. Every operation therefore takes
effect at a single point inside a serialized batch.

The combiner does reorder within a batch. Pushes run first, pops are deferred to
a second pass, and a deferred pop can return `data.back()` instead of the heap
root when the batch's last push outranks it. Every operation in a batch overlaps
every other in real time, so any order over the batch is an admissible
linearization, and the reordering stays inside that freedom. It is not an
ordering guarantee TBB documents, but it is what makes the container pass the
strict `spsc`, `mpsc`, and `spmc` gates and not only the relaxed `mpmc` one.

## Limitations

- **`VSPQ_INVALID` is not externally atomic.** TBB cannot peek, so an undersized
  output is only detectable after the item has been removed. The adapter
  requeues the untouched entry and returns `VSPQ_INVALID`, which leaves a window
  where the item is invisible to other consumers. The contract asks for the item
  to stay queued. This passes today only because the evaluator issues undersized
  dequeues from the sequential ABI probe, never from the concurrent history, so
  the window is unobservable. A candidate that peeks before committing, as the
  locked-heap control does, is strictly more correct here.
- **Not lock-free.** Flat combining blocks: a descheduled combiner stalls every
  operation in its batch. The try-style requirement is still met, since neither
  call ever waits on queue state (for space, or for an item), but this is a
  blocking structure like the locked-heap control, not a lock-free one.
- **Reservation window.** Taking a slot before publishing, and holding one while
  copying a dequeued value out, is a capacity reservation. The contract permits
  this explicitly for `mpmc`. For the strictly linearizable scenarios both
  windows sit inside the enclosing call, and the windows scale with
  `--value-size`.

## Measured

Median successful ops/s over 3 repetitions, default `--capacity 1024` and
`--value-size 8`, on a 256-core machine. Absolute values are machine-specific;
the ratios are the point.

| scenario | locked-heap | sharded-heap | oneTBB | oneTBB vs. best |
| --- | --- | --- | --- | --- |
| `spsc` | 2147349 | 1666962 | 1762788 | 0.8x |
| `mpsc` | 674932 | 566260 | 1675563 | 2.5x |
| `spmc` | 767021 | 714362 | 1099051 | 1.4x |
| `mpmc` | 808357 | 872443 | 2229637 | 2.6x |

Treat these as one significant figure. `mpsc` in particular is noisy across
repetitions for every implementation: five 3-repetition runs of oneTBB produced
medians from 1.3M to 2.2M, so the row above is the median of those medians
rather than a single run.

TBB wins where the contention is real, and it is the only one of the three that
does not collapse in `mpsc`. It loses to the locked-heap control on `spsc`,
where flat combining pays for machinery no single-threaded pair needs. `spmc` is
its weakest contended case: every consumer serializes through the combiner, so
adding consumers buys less than adding producers does.

## Running it

This directory is deliberately outside the `prioqueue-*` input bundles. VibeSys
does not copy it into optimization workspaces, so the optimization agents cannot
inspect or reuse the implementation.

From this directory:

```bash
make
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-tbb --scenario spsc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-tbb --scenario mpsc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-tbb --scenario spmc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-tbb --scenario mpmc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-tbb --scenario mpmc \
  --operations 24 --trials 100 --capacity 4 --value-size 64
go -C ../../evaluators/priority-queue run . benchmark \
  --workspace ../../baselines/prioqueue-tbb --scenario mpmc \
  --repetitions 3
```
