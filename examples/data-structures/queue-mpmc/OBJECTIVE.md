Optimize a multi-producer, multi-consumer bounded FIFO queue.

Preserve the required interface:
- Provide an executable `./queue-candidate` launcher.
- Accept the protocol v1 arguments and inherited socket descriptors documented in
  `_input_libs/queue-input-core/QUEUE_PROTOCOL.md`.
- Implement enqueue and dequeue for unsigned 64-bit values using the capacity
  and MPMC scenario supplied by trusted launcher arguments.

The candidate may use any language or combination of languages. The queue must
remain linearizable, must not fabricate or duplicate items, and must respect
capacity. Maximize trusted end-to-end operation throughput for the MPMC
workload.
