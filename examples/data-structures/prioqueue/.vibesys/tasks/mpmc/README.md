# Priority Queue MPMC Task

This task targets a multi-producer, multi-consumer reservation-aware bounded
priority queue. Successful enqueues may reserve capacity before publication;
`FULL` counts reservations while `EMPTY` and dequeue observe published items.
The manifest invokes the shared trusted priority queue evaluator and ABI.

The project root holds an intentionally naive Rust candidate. From the project
root, run:

    make
    go -C <vibesys>/resources/evaluators/priority-queue run . check --workspace "$PWD" --scenario mpmc
    go -C <vibesys>/resources/evaluators/priority-queue run . benchmark --workspace "$PWD" --scenario mpmc --duration 1s --warmup 0s

The candidate is untrusted and may be replaced by any implementation exporting
the ABI in the evaluator package's `CANDIDATE_CONTRACT.md`
(`resources/evaluators/priority-queue` in the VibeSys repository).
