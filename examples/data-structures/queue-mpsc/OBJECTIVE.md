Optimize a multi-producer, single-consumer bounded FIFO queue.

Preserve the required interface:
- Provide a native shared library named `./queue-candidate.so`.
- Export the copying C ABI documented in
  `_input_libs/queue-input-core/QUEUE_ABI.md`.
- Implement enqueue and dequeue for copied byte values using the capacity and
  value size supplied by the trusted runner.

The candidate may use any language or combination of languages. The queue must
remain linearizable, must not fabricate or duplicate items, and must respect
capacity. Maximize trusted end-to-end operation throughput for the MPSC
workload.
