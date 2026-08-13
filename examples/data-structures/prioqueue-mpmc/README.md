# Priority Queue MPMC Input

This input targets a multi-producer, multi-consumer reservation-aware bounded
priority queue. Successful enqueues may reserve capacity before publication;
`FULL` counts reservations while `EMPTY` and dequeue observe published items.
The manifest invokes the shared trusted priority queue evaluator and ABI.

The shared seed at `examples/starters/priority-queue-rs` is an intentionally
naive Rust candidate. From a materialized workspace, run:

    make
    go -C _evaluator/priority-queue run . check --workspace "$PWD" --scenario mpmc
    go -C _evaluator/priority-queue run . benchmark --workspace "$PWD" --scenario mpmc --duration 1s --warmup 0s

The starter is untrusted and may be replaced by any implementation exporting
the ABI in `_evaluator/priority-queue/CANDIDATE_CONTRACT.md`.
