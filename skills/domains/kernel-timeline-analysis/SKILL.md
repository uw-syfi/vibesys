---
name: kernel-timeline-analysis
description: Instrument a CUDA kernel with FlashInfer's profiler macros and read its per-block timeline. Use for persistent, multi-phase, or grid-synchronized kernels where per-launch counters cannot say which phase or which block sets the critical path.
---

# Kernel timeline analysis

Kernel-level counters average over the whole launch. A persistent kernel, a
megakernel, or a kernel with grid-wide synchronization spends its time in a
chain of phases, and what limits it is the slowest block in each phase plus
the wait at each synchronization. A per-block timeline measures that chain.

Instrument with FlashInfer's `include/flashinfer/profiler.cuh`: `PROFILER_INIT`
once per block or warp group, then `PROFILER_EVENT_START` and
`PROFILER_EVENT_END` around each phase, with event indices that map to a list
of names. The macros compile to nothing unless `FLASHINFER_ENABLE_PROFILER` is
defined, so keep the probes in the candidate and build an instrumented copy only
to profile. Put a probe at each phase boundary that carries a dependency: a grid
or group barrier, a weight or KV load, a cross-block reduction. Leave the
synchronization outside the phase it ends, so its wait shows up as idle time.

Read the timeline per phase over all blocks, not as one average:

- The slowest block's time well above the mean is load imbalance.
- Phase wall time well above the slowest block's time is start skew or a slow
  release after synchronization.
- The block that finishes last is the one the next dependency waits for. If
  the same block is always last, fix its share of the work; if it moves around,
  the cost is contention or synchronization.
- A change can move cost from one phase to another. Compare every phase, before
  and after, not only the one you changed.

To say how much each phase costs the launch, take its critical-path window. The
k-th occurrence of a phase on every block forms one occurrence. Order the
occurrences by their latest end over all blocks. An occurrence's window is its
latest end minus the latest end of anything before it (the timeline's start
for the first), and never below zero. Sum the windows per phase. The windows of
all phases then sum to the span, so a phase's window is the time the launch
would save if that phase took none. A phase whose window is much smaller than
its own duration runs in the shadow of another phase, and speeding it up alone
gains little.

Compare windows only between timelines with the same probes, and repeat each
side at least three times. The spread between repeats of one build is the
smallest change a window comparison can show.

Probes perturb what they measure: each adds a timer read and a store, and a
dense set of probes can shift where a memory burst lands. Use the timeline to
choose the next experiment, and the task's unprofiled benchmark, timed against
a control in the same session, to accept it.
