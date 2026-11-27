# CPU+GPU co-execution for prefill on Apple Silicon

Split prefill matmuls across the CPU and GPU of one Apple Silicon SoC so both units compute at once. Applies to prefill only; decode gains nothing. How to split depends on whether the weights are quantized.

## Prerequisites

- A precision-matched baseline: the same weights and dtype on GPU-only and split runs.
- A GPU-only reference output to check the split against.
- Memory headroom for the CPU's FP16 slices when the model is quantized (see Pitfalls).

## Result on the 8-bit example model

`mlx-community/Meta-Llama-3.1-8B-Instruct-8bit` (revision `142d428`, the model in `examples/model-serving/Llama-3.1-8B-Instruct-MLX-8bit`), stock MLX-LM with `QuantizedLinear` patched for prefill-sized calls. Apple M4, 24 GB, macOS 27.0, MLX 0.31.2, mains power, median of 5 runs:

| Prompt | Stock TTFT | Column split TTFT | Speedup |
|:--|:--|:--|:--|
| 512 tokens | 2,247 ms | 1,879 ms | 1.20x |
| 1,024 tokens | 4,517 ms | 3,688 ms | 1.22x |
| 2,048 tokens | 9,338 ms | 7,650 ms | 1.22x |

The first 48 greedy tokens were identical to stock at every length. Decode calls bypass the split, so decode throughput is unchanged (about 11.7 tok/s). Peak memory rose from 9.1 GB to 13.0 GB.

For a workload scored on end-to-end latency of long outputs (such as 256-token JSON responses), prefill is a small part of each request; expect a few percent there, not 1.2x.

## Concept

The CPU and GPU share one memory system, so one matmul can be split between them with no copy.

| Weights | Split | Why |
|:--|:--|:--|
| FP16 / BF16 | **Row split**: some input rows on `mx.cpu`, the rest on `mx.gpu` | Both units run the same FP16 matmul |
| MLX-quantized (`QuantizedLinear`) | **Column split**: some output columns on `mx.cpu` from an FP16 copy of just those columns; the rest stays quantized on `mx.gpu` | MLX's CPU `quantized_matmul` has no fast path (below) |

### Quantized weights: row split fails

On an M1 (8 GB, macOS 27.2, MLX 0.31.2), one 8-bit, group-size-64, 4096x14336 matmul:

| Rows | GPU only | CPU only | Row split, 5% rows on CPU |
|:--|:--|:--|:--|
| 512 | 34.5 ms | 5,611 ms | 274 ms (0.13x) |
| 2048 | 139.5 ms | killed after 120 s | 1,116 ms (0.13x) |

A CPU-stream piece that slow can stall the GPU queue until Metal aborts the process (`kIOGPUCommandBufferCallbackErrorTimeout`). Never row-split quantized weights.

### FP16 weights: row split works

Same M1, Llama-3-8B-geometry FP16 decoder block, 30% of rows on the CPU: 1.26x, 1.30x and 1.17x faster than GPU-only at 1,024, 2,048 and 4,096 tokens (fanless chassis, on battery; spread grew at 4,096). Fewer rows lose: on one 4096x14336 FP16 matmul, 16 rows ran at 0.34x, 128 at 0.68x, 512 at 1.27x.

## Workflow

### 1. Patch the prefill path

```python
def column_split(self, x):                       # QuantizedLinear.__call__ for prefill
    rows = math.prod(x.shape[:-1])
    if rows < MIN_ROWS or self._n == 0:          # decode and tiny calls: stock path
        return stock_call(self, x)
    n = self._n                                   # CPU's columns, a multiple of 64
    x2 = x.reshape(rows, -1)
    mx.eval(x2)                                   # eager boundary (see step 3)
    c = mx.matmul(x2, self._w_fp16_slice, stream=mx.cpu)            # (in, n), built once
    g = mx.quantized_matmul(x2, self.weight[n:], self.scales[n:], self.biases[n:],
                            transpose=True, group_size=self.group_size,
                            bits=self.bits, stream=mx.gpu)
    y = mx.concatenate([c, g], axis=1)
    return y.reshape(*x.shape[:-1], -1)
```

Build `_w_fp16_slice` once with `mx.dequantize` on the first `n` output rows of the weight, transposed. `MIN_ROWS = 256` routed every decode call to the stock path.

### 2. Calibrate the CPU share per weight shape

Time the **actual concurrent split** over a grid (0, 0.10, ..., 0.45) for each distinct weight shape at the prompt lengths you serve. Solo CPU and GPU timings overstate the CPU's share, because the units share bandwidth. Measured on the M4 for Llama-3.1-8B 8-bit shapes at 2,048 tokens (the end-to-end run above used q/o and gate/up 0.30, down 0.20, k/v 0):

| Layer | Shape (out x in) | Best CPU share | Speedup at that share |
|:--|:--|:--|:--|
| q, o | 4096 x 4096 | 0.30 (0.20 at 512 tokens) | 1.33x |
| gate, up | 14336 x 4096 | 0.30 | 1.38x |
| down | 4096 x 14336 | 0.20 (0.30 at 512 tokens) | 1.17x |
| k, v | 1024 x 4096 | 0 (skip) | 1.04x at best |

The down projection is the sensitive one: past its best share it loses fast (0.91x at 0.25 on 2,048 tokens). A single share for every layer cost most of the gain at 2,048 tokens on the M1.

### 3. Decide on the eager boundary by measuring

`mx.eval` on the split's input before the two stream ops guarantees the CPU and GPU work from a materialized array. On the M4 run it gave steadier results (1.20x, 1.22x, 1.22x) than leaving it out (1.18x, 1.25x, 1.13x); on the M1, an FP16 block was equal either way. Measure both on the target machine.

## Where co-execution does not help

| Case | Why |
|:--|:--|
| Decode | Each token streams the full weight set once and the bandwidth is shared; a second unit adds compute, not bandwidth. |
| Short prompts or few rows | The CPU share is too small to pay for the split. |
| Row split of quantized weights | CPU `quantized_matmul` is about 160x slower than the GPU. |
| Machines without memory headroom | The FP16 slices push an 8 GB machine into swap; on the M1 a 3B 8-bit model got slower, including decode. |

## Pitfalls

- **Memory.** The FP16 slices add about `share x 2 bytes` per split parameter: 9.1 GB to 13.0 GB peak for the 8B 8-bit model. Check swap before and after; any swap growth invalidates the timing.
- **Keeping extra weight copies.** Slice the quantized weight per call (`self.weight[n:]`) instead of storing a second quantized copy; a stored copy is what pushed the first M1 attempt into swap.
- **One share for every layer.** Calibrate per shape; the down projection needs less.
- **Precision-mismatched baselines.** Compare against the same quantized model on the GPU, not an FP16 model; any unmatched speedup is reward hacking.
- **Expecting bit-identical logits.** Part of each layer is computed in FP16 instead of through the quantized kernel. Check generated tokens and a relative-error bound (about 1e-3 per layer here).
- **Busy CPUs.** Other processes on the CPU cores take the share the split depends on. Benchmark with other apps closed and on mains power.

## See also

- [`floor.md`](floor.md) — the Metal floor; §2 covers lazy evaluation in the decode loop, §5 quantization
- [`async-scheduling.md`](async-scheduling.md) — host/device overlap, the other use of streams
- [`hardware.md`](hardware.md) — bandwidth per SKU, which bounds decode
