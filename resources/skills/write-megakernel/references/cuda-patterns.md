# CUDA patterns with explicit lifetimes

Use [protocols.cuh](../examples/protocols.cuh) for the complete teaching helpers
and [protocol_smoke.cu](../examples/protocol_smoke.cu) for their executable test.
The snippets below omit application arithmetic. They are scheduling patterns,
not a complete decoder or a recommendation for a particular tile size.

## Publish a payload and acknowledge its final reader

For each edge, write down: producer CTAs, consumer CTAs, payload region, ready
flag, first publication, last reader, and permission to reuse the region.

This channel has exactly one producer CTA and one consumer CTA. Both flags
start at zero inside the cooperative kernel, followed by one grid barrier.
Every participating thread executes these calls in the same order:

```cuda
// Producer, epochs 1..N:
await_cta(&channel.consumed, epoch - 1);
// All producer threads write their disjoint pieces of payload.
publish_cta(&channel.ready, epoch);

// Consumer, epochs 1..N:
await_cta(&channel.ready, epoch);
// All consumer threads read payload and produce their outputs.
publish_cta(&channel.consumed, epoch);
```

`publish_cta` joins the CTA, then its leader performs a device-scope release
store. `await_cta` has the leader acquire-load the flag, then joins the CTA before
payload reads. Both joins matter when other threads access payload. A flag alone
does not make arbitrary payload writes visible. See NVIDIA's
[CUDA memory model](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/cuda-cpp-memory-model.html).

The acknowledgment is the reuse rule. Without it, a fast producer can overwrite
payload while the consumer still reads it, or advance an exact-equality flag
past the epoch a consumer awaits. Replacing equality with `>=` does not recover
overwritten data. A double-buffered channel still needs one lifetime protocol
per buffer. Do not allow integer epochs to wrap.

For several consumer CTAs, track each reader's completion or use a proven join
that accounts for all of them. One consumer's acknowledgment is insufficient.
The example uses two independent producer channels, with one consumer acquiring
both. This avoids relying on a relaxed shared counter to publish other CTAs'
writes. For a counter, use the more restricted protocol below.

Release/acquire makes an implemented dependency visible. It cannot justify
removing a mathematical dependency: a dense projection still needs every input
column, and a residual buffer cannot change while another CTA is reading it.

### Optional: a one-shot stage completion counter

Use this only when the consumer requires all `producer_count` disjoint outputs.
Initialize `done = 0` inside the cooperative kernel and execute a grid barrier
before this stage. Each producer increments exactly once, after every thread has
finished its payload writes and any asynchronous operations. No other code writes
the counter; it cannot overflow, be reset, or be reused during this invocation.

```cuda
// Every producer CTA, once after writing its disjoint payload:
__syncthreads();
if (threadIdx.x == 0) {
    cuda::atomic_ref<int, cuda::thread_scope_device>(*done)
        .fetch_add(1, cuda::memory_order_acq_rel);
}

// Every consumer CTA, before reading any producer's payload:
if (threadIdx.x == 0) {
    while (cuda::atomic_ref<int, cuda::thread_scope_device>(*done)
               .load(cuda::memory_order_acquire) != producer_count) {}
}
__syncthreads();
```

Each acquire/release RMW reads the preceding RMW's value, carries its publication
forward, and adds this producer's writes. Acquiring the final count observes that
chain; the consumer CTA barrier extends visibility to its readers. Do not replace
the RMWs with relaxed operations without a separate memory-model argument.
Keep payload immutable until all consumers finish. This counter publishes
producers; it does **not** acknowledge readers or permit storage reuse.

This illustrative counter is not exercised by `protocol_smoke.cu`. Use the tested
per-channel acknowledgment protocol for repeated epochs unless the task supplies
and verifies an equally explicit counter and payload lifecycle.

## Guarantee progress before introducing spins

`check_residency` checks cooperative-launch support and computes resident blocks
from the compiled kernel, block size, and dynamic shared-memory request. Launch
through `cudaLaunchCooperativeKernel` only if the full grid fits. Configure any
shared-memory opt-in before querying occupancy. Recheck after changing registers,
shared memory, launch bounds, or block size. NVIDIA documents this requirement in
[grid synchronization](https://docs.nvidia.com/cuda/archive/12.9.0/cuda-c-programming-guide/index.html#grid-synchronization).

Residency prevents consumers from occupying every slot while awaited producers
remain queued. It does not fix dependency cycles, divergent CTA barriers, or
missing publications. The runnable example uses three resident CTAs and fresh
per-launch workspace; it adds no separate initialization kernel to the pipeline.

If the required grid cannot reside simultaneously, redesign the persistent worker
schedule or split the launch if the task permits. Do not spin across waves of an
ordinary oversubscribed grid.

## Bulk copies: issue, complete, consume, release the buffer

The header's PTX helpers target SM90+ and compile for the B200 with `sm_100`.
Use 16-byte-aligned source/destination addresses and transfer sizes divisible by
16. Both the shared barrier and destination belong to the issuing CTA. This
example uses no cross-CTA shared memory or cluster multicast.

Initialize each shared `mbarrier` once with one arrival, fence its initialization
to the async proxy, and join the CTA. For each transaction the leader:

1. Fences prior generic shared accesses before asynchronous buffer reuse.
2. Arrives while registering the expected byte count.
3. Issues the bulk copy with that barrier as its completion target.
4. Waits for completion of the current phase, toggles its expected parity, and
   joins the CTA.

Only after that join may other threads read the tile or publish results derived
from it. The source remains valid until completion. See NVIDIA's
[async-copy guide](https://docs.nvidia.com/cuda/archive/13.2.1/cuda-programming-guide/04-special-topics/async-copies.html)
and [PTX bulk-copy semantics](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#data-movement-and-conversion-instructions-cp-async-bulk).

Each of the two buffers has its own barrier. The executable uses this order:

```cuda
bulk_init_cta(barrier[0]);
bulk_init_cta(barrier[1]);
bulk_begin_cta(barrier[0], tile[0], source, bytes);
for (int j = 0; j < tile_count; ++j) {
    int current = j % 2;
    bulk_wait_cta(barrier[current]);
    if (j + 1 < tile_count) {
        int next = 1 - current;
        bulk_begin_cta(barrier[next], tile[next], next_source(j), bytes);
    }
    // Consume tile[current] while the other buffer is being filled.
    __syncthreads();  // Every reader finishes before this buffer is reused.
}
bulk_finish_cta(barrier[0]);
bulk_finish_cta(barrier[1]);
```

The barrier remains live across transfers, with independent phase tracking per
buffer. Invalidate it only after all transfers and readers finish. The executable
also keeps barriers live across epochs. Do not reset phase when advancing an
epoch. Overlap helps only if the extra buffers preserve sufficient
residency and the workload has useful work to execute during the transfer.

## Merge split attention with stable normalization

For partition `p`, save maximum `m_p`, denominator `l_p`, and **unnormalized**
weighted-value numerator `u_p`:

```text
m_p = max(score_i)
l_p = sum(exp(score_i - m_p))
u_p = sum(exp(score_i - m_p) * value_i)

m = max_p(m_p)
l = sum_p(exp(m_p - m) * l_p)
u = sum_p(exp(m_p - m) * u_p)
attention_output = u / l
```

`merge_softmax` implements the pairwise merge and treats zero-denominator empty
partitions as identities. The caller must handle an entirely masked row according
to the task's contract before dividing. The helper assumes finite unmasked
scores. Floating-point merge order can change results; check the task tolerances
and accumulation dtype. Every partition's summary needs publication before the
merge, and its storage needs reader completion before reuse.

Partitioning trades more producer CTAs against extra summary writes, merge work,
and readiness edges. Sweep the partition count with full-model timing; do not
derive it solely from sequence length or SM count.

## Build and verify the example

From the VibeSys repository root, with `nvcc` on `PATH`:

```bash
nvcc -std=c++17 -O3 -lineinfo -arch=sm_100 \
  resources/skills/write-megakernel/examples/protocol_smoke.cu \
  -o /tmp/write-megakernel-protocol-smoke
```

`sm_100` is the tested B200 target. Select the actual GPU target when adapting
the example; its bulk-copy helpers require SM90 or newer.

Run `/tmp/write-megakernel-protocol-smoke` inside a Slurm GPU allocation, followed
by `compute-sanitizer --tool memcheck`, `--tool synccheck`, and `--tool racecheck`
with `--error-exitcode 1` on the same executable. Compilation needs no GPU.

The executable checks 12 pipeline cases (1, 2, 17, and 257 epochs, three input
seeds, five tiles per producer) against exact CPU sums, with deliberately dirty
workspace. It also checks all 14 splits of a 13-score softmax, including empty
partitions and large scores, against a double-precision reference. Each pipeline
invocation uses one cooperative launch. The executable runs multiple test cases
and a separate softmax test; it is not itself a one-launch benchmark submission.

A sanitizer pass is useful evidence, not a proof of every cross-CTA dependency.
Review the memory-order and lifetime argument above whenever adapting the code.

Validated on a B200 with CUDA compiler 13.1.115 through Slurm job 4155 on
2026-10-06: all 12 pipeline cases and 14 softmax splits passed, with zero memcheck
or synccheck errors and zero racecheck hazards or warnings. This covers the
executable and header; the optional counter snippet above remains illustrative.
