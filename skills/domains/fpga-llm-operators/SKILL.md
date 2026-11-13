---
name: fpga-llm-operators
description: HLS design patterns for LLM inference operators on FPGAs, such as matrix-vector and matrix multiply, with integer and fixed-point arithmetic. Use when implementing or optimizing a GEMV, GEMM or tutorial matmul or convolution kernel in HLS.
---

# FPGA LLM operators

LLM decode at small batch is dominated by matrix-vector multiplies, which read
every weight once per token. Such kernels are usually bound by how fast
weights arrive, not by arithmetic, so design the memory path first.

## Matrix-vector and matrix multiply

- **Stream the weights.** Read the weight matrix in wide, sequential bursts
  and consume it as it arrives; keep the input vector on chip, partitioned so
  each parallel lane reads its element in the same cycle.
- **Choose the parallelism from the bandwidth.** The useful number of
  parallel multiply-accumulate lanes is roughly the weights delivered per
  cycle. More lanes than that idle; fewer leave bandwidth unused.
- **Unroll the inner product and pipeline the outer loop.** Partition the
  arrays the unrolled loop reads by the unroll factor, or the loop is limited
  by memory ports, not arithmetic.
- **Reduce with a tree, accumulate across iterations carefully.** A sum that
  feeds back into itself every iteration limits II to the adder's latency for
  floating point; integer accumulation does not. Use partial sums or integer
  or fixed-point accumulators to keep II at 1.
- **Tile for reuse when the batch is larger than one.** With several input
  vectors, each weight read can serve all of them, which moves the bound from
  bandwidth toward compute.

## Arithmetic widths

- Use `ap_int<N>`, `ap_uint<N>` and `ap_fixed<W, I>` at the width the contract
  specifies, not wider: width sets the logic each multiplier and adder costs.
- An accumulator must be wide enough for the whole sum without overflow, for
  example 8-bit by 8-bit products summed over K terms need about
  16 + log2(K) bits.
- Narrow multiplies can share one DSP; very narrow ones (a few bits) are often
  cheaper in LUTs. Check which the synthesis report chose.

## Tutorial kernels

A matmul or convolution tutorial task exercises the same levers: loop order,
pipelining, unrolling with matching partitioning, and a line or tile buffer for
reuse. Get it correct in C simulation first, then change one lever at a time
and read the effect in the synthesis report.
