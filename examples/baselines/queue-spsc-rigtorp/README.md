# Rigtorp-style SPSC baseline

This is the comparison baseline for the `queue-spsc` optimization input. It
specializes Erik Rigtorp's
[`SPSCQueue`](https://github.com/rigtorp/SPSCQueue) algorithm for the VibeSys
copying byte-value ABI:

- one preallocated circular buffer with one slack slot;
- producer-owned and consumer-owned indices on separate cache lines;
- cached copies of the remote index to reduce coherency traffic; and
- release publication paired with acquire observation.

The specialization stores payload bytes directly in fixed-stride slots. Using
the generic C++ container with `std::vector<std::byte>` would add a heap
allocation to every successful enqueue and would not be a representative
baseline for this fixed-capacity contract.

The adapted algorithm retains Rigtorp's MIT license in `LICENSE.rigtorp`.

This directory is deliberately outside the `queue-spsc` input bundle. VibeSys
does not copy it into optimization workspaces, so the optimization agents cannot
inspect or reuse the implementation.

From this directory:

```bash
make
go -C ../../evaluators/queue run . check \
  --workspace ../../baselines/queue-spsc-rigtorp --scenario spsc
go -C ../../evaluators/queue run . benchmark \
  --workspace ../../baselines/queue-spsc-rigtorp --scenario spsc \
  --repetitions 3
```
