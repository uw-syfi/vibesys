# Locked-heap priority queue baseline

This is the conservative control baseline for the `priority-queue` evaluator. It
holds a fixed, preallocated array-based binary min-heap and serializes every
operation, including the payload copy, under one mutex. Nothing is reserved
before it is published and nothing is published before it is visible, so each
operation is externally atomic and the implementation satisfies the strict
linearizable contract as well as the relaxed MPMC one.

- one preallocated payload arena of `capacity * max_value_size` bytes;
- heap entries of `(priority, slot)` so sifting never moves value bytes;
- the slot column is a permutation of `[0, capacity)`, so the entry just past
  the live prefix is always free storage for the next push, and no separate
  free list is needed; and
- `VSPQ_INVALID` is decided from the heap root before the pop, so an undersized
  output leaves the item queued and the caller's storage untouched.

Valid for all four scenarios: `spsc`, `mpsc`, `spmc`, and `mpmc`.

The single mutex is also the point of the baseline: it is the throughput floor
that any sharded, lock-free, or copy-outside-the-lock design has to beat. Value
copying happens inside the critical section, so its cost scales with
`--value-size`.

This directory is deliberately outside the `prioqueue-*` input bundles. VibeSys
does not copy it into optimization workspaces, so the optimization agents cannot
inspect or reuse the implementation.

From this directory:

```bash
make
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-locked-heap --scenario spsc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-locked-heap --scenario mpsc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-locked-heap --scenario spmc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-locked-heap --scenario mpmc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-locked-heap --scenario mpmc \
  --operations 24 --trials 100 --capacity 4 --value-size 64
go -C ../../evaluators/priority-queue run . benchmark \
  --workspace ../../baselines/prioqueue-locked-heap --scenario mpmc \
  --repetitions 3
```
