Optimize a single-producer, single-consumer bounded FIFO queue.

Preserve the required interface:
- Provide an executable `./queue-candidate` launcher.
- Accept `--vibeserve-queue-shm <path>` and serve protocol v1 from
  `_input_libs/queue-input-core/QUEUE_PROTOCOL.md`.
- Implement enqueue and dequeue for unsigned 64-bit values using the capacity
  and SPSC scenario declared by the trusted shared-memory header.

The candidate may use any language or combination of languages. The queue must
remain linearizable, must not fabricate or duplicate items, and must respect
capacity. Maximize trusted end-to-end operation throughput for the SPSC
workload.
