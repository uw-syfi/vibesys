# Locked-ring MPMC baseline

This is the hidden exact-contract baseline for `queue-mpmc`. It stores copied
values in a fixed, preallocated ring and serializes reservation, copying, and
publication with one mutex. It serves as the simple exact-contract correctness
and performance control.

The serialization is intentional. Reserve-then-publish rings such as a direct
SCQD or bounded sequence-ring adapter can expose a completed `FULL` operation
followed by a later `EMPTY` while a successful enqueue owns the only free slot
but has not published its copied bytes. The queue ABI has no `BUSY` result, so
that history is not linearizable. This baseline keeps each operation's state
transition atomic with respect to every other operation and remains a simple
portable implementation verified against the reservation-gap litmus.

The directory is outside the `queue-mpmc` input bundle and is not copied into
optimization workspaces.

```bash
make
go -C ../../evaluators/queue run . check \
  --workspace ../../baselines/queue-mpmc-locked-ring --scenario mpmc \
  --operations 24 --trials 100
go -C ../../evaluators/queue run . benchmark \
  --workspace ../../baselines/queue-mpmc-locked-ring --scenario mpmc \
  --repetitions 3
```
