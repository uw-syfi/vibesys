---
name: kernel-ncu-analysis
description: Interpret NVIDIA Nsight Compute evidence for a CUDA kernel task. Use when an NCU capture is available and a measured bottleneck needs diagnosis.
---

# Kernel NCU analysis

Match each captured launch to the benchmarked candidate, input shape, dtype,
device, and warmup state. Multiple launches with similar names may represent
different phases; inspect launch parameters and source correlation before
attributing cost. NCU replay and counter collection can perturb timing, so use
the task benchmark for before and after performance decisions.

Form one hypothesis from the relevant counters. Low achieved occupancy can
follow register or shared-memory pressure, but low occupancy alone does not
prove it limits throughput. Memory throughput and cache hit rates need byte
counts and access patterns to be meaningful. Warp stalls identify where warps
wait; relate them to the instruction mix, dependency chain, and memory traffic
before choosing a change.

Report the capture identity, dominant launches, metric values, uncertainty or
missing counters, and the next experiment. Confirm any optimization with the
accuracy checker and scored benchmark.
