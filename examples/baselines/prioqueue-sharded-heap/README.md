# Sharded-heap priority queue baseline

This is the contended-fast-path baseline for the `priority-queue` evaluator. It
splits the queue into per-shard locked binary min-heaps, one shard per producer
(capped at 16). Producers push into their own shard, so producer-side pushes do
not contend with each other. Consumers scan the shards for the smallest
priority and pop from the winning shard, so pops from different shards proceed
in parallel.

Capacity is global. A shared pool of `capacity` payload slots is the capacity
token: a producer takes a slot before copying and a consumer returns a slot
after copying, so `VSPQ_FULL` is reported only when all `capacity` slots are
held, and both `memcpy` calls run outside every lock. This is the second point
of the baseline: the locked-heap control copies under its one mutex, this one
does not.

Valid for all four scenarios: `spsc`, `mpsc`, `spmc`, and `mpmc`.

## Why the scan is linearizable

A one-shard-at-a-time scan does not observe a globally atomic minimum, so the
naive version of this design is wrong: a lower priority can be inserted into a
shard the scan already passed, and another consumer can drain the winning shard
between the scan and the pop. The first version of this baseline failed the
`mpmc` gate for exactly the second reason.

The fix is to re-validate under the winning shard's lock. The scan records the
best shard and the best priority seen in any *other* shard. The pop commits only
while the winning shard is non-empty and its current root still beats that
runner-up; otherwise the whole scan restarts. That makes the popped priority no
worse than everything the scan observed. Any shard whose minimum dropped below
it did so through an insertion that happened after the scan read that shard,
therefore inside this dequeue call, so the two operations overlap and the missed
enqueue is still orderable after the dequeue.

The restart is not a wait on another operation: it re-reads current state and
only happens when another consumer has already made progress.

## Limitations

- Sharding only pays off when producers and consumers are both plural. It is
  measurably slower than the locked-heap control in the other three scenarios:
  `spsc` and `spmc` have one producer and therefore one shard, so the baseline
  degenerates to the control plus a second lock for the slot pool, and in `mpsc`
  the single consumer is the bottleneck and now pays a shard scan per dequeue.
- The slot pool is a single mutex shared by all producers and consumers. Its
  critical section is a few instructions, but it is the residual serialization
  point and the obvious next thing an optimizing candidate would remove.
- Taking a slot before publishing the item is a capacity reservation, and so is
  holding a slot while copying a dequeued value out. The contract allows this
  explicitly for `mpmc`. For the strictly linearizable scenarios both windows
  sit inside the enclosing call, which keeps the history linearizable, but the
  windows scale with `--value-size`.

This directory is deliberately outside the `prioqueue-*` input bundles. VibeSys
does not copy it into optimization workspaces, so the optimization agents cannot
inspect or reuse the implementation.

From this directory:

```bash
make
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-sharded-heap --scenario spsc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-sharded-heap --scenario mpsc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-sharded-heap --scenario spmc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-sharded-heap --scenario mpmc \
  --operations 24 --trials 100
go -C ../../evaluators/priority-queue run . check \
  --workspace ../../baselines/prioqueue-sharded-heap --scenario mpmc \
  --operations 24 --trials 100 --capacity 4 --value-size 64
go -C ../../evaluators/priority-queue run . benchmark \
  --workspace ../../baselines/prioqueue-sharded-heap --scenario mpmc \
  --repetitions 3
```
