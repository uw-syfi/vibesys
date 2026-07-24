# vLLM H100 Workload Tuning Notes

This summarizes the three Llama 3.1 8B H100 optimization loops run with Modal and GPT-5.5 agents. Each workload was materialized as a separate input bundle under `examples/model-serving/`:

- `llama-3-8b-h100-long-prompts`
- `llama-3-8b-h100-high-concurrency`
- `llama-3-8b-h100-constrained-json`

The per-round logs remain in each experiment's `workspace/progress.md`; this file records the headline process and accepted results.

## Workload Results

| Workload | Baseline used | Best robust throughput | Speedup |
| --- | ---: | ---: | ---: |
| Long prompts, short outputs | 508.53 tok/s robust stock rerun | 512.53 tok/s robust long-specialized rerun | 1.01x |
| High concurrency, many short outputs | 1,035.73 tok/s robust stock rerun | 1,147.47 tok/s robust high-specialized rerun | 1.11x |
| Constrained JSON decoding | 934.33 tok/s robust stock rerun | 1,353.18 tok/s robust code+FlashInfer ablation | 1.45x |

Notes:

- The long-prompt loop also had an early framework number of 6.94 tok/s from a cold/service-path artifact. I do not use that as the meaningful baseline.
- The long-prompt final Round 10 implementation had a self-benchmark at 301.05 tok/s but the judge loop exhausted due stale/failed judge evaluation; the best accepted framework result remains 302.54 tok/s from Round 3.
- The constrained JSON loop's judge benchmark reached 450.44 tok/s; the official framework benchmark accepted 427.20 tok/s.
- The robust reruns below supersede the original headline throughput claims. The earlier numbers were useful for search direction, but the corrected measurements count true completion tokens, use fixed-duration multi-trial windows, and gate on a single Modal H100 80GB container per run.

## Long Prompts

Hypothesis: long prompts with short completions should benefit most from vLLM prefix caching, chunked prefill, stable CUDA graph capture sizes, and readiness warmup that exercises the actual prompt pool.

Expected: improve TTFT and request throughput by avoiding cold compile/cache effects and by keeping batch-size 16 graph coverage available.

Changes that helped:

- Kept `max_num_batched_tokens=4096`, chunked prefill, prefix caching, and CUDA graph sizes `[1, 2, 4, 8, 16]`.
- Added real readiness warmup over the exact 64-prompt benchmark pool.
- Batched the warmup as four list-valued `/v1/completions` requests of 16 prompts each.
- Added list-prompt handling in the OpenAI-compatible completions endpoint so the warmup executed distinct vLLM generations instead of collapsing to one prompt.

What did not hold up:

- A greedy sampler fast path was noisy and regressed framework validation.
- Shared/proxy streaming client experiments were unstable.
- A 16-request streaming warmup caused severe p99/throughput variance; reducing to four looked better in self-benchmark but was not accepted by the final judge loop.

Actual best accepted framework-loop result: 302.54 tok/s.

The robust one-H100 rerun below supersedes this historical loop number for final performance claims.

Why it improved: this workload has extreme prompt-prefix reuse. Real warmup and prefix caching moved repeated 3000-word prompts onto the hot path, while chunked prefill and graph capture kept long-prefill/decode scheduling stable at concurrency 16.

## High Concurrency

Hypothesis: the high-concurrency short-output case should be limited less by single-request GPU math and more by admission, bridge readiness, and whether Modal/vLLM actually sees enough concurrent work to continuously batch.

Expected: large gains from ensuring one warm H100/vLLM engine receives the full concurrency instead of serializing at the bridge or spinning cold containers.

Changes that helped:

- Restored a warm single-step scheduler path with async scheduling off by default.
- Ensured the Modal class admits benchmark concurrency with `@modal.concurrent(max_inputs=64)`.
- Added deterministic bridge readiness: verify current backend, warm with real non-streaming and streaming requests, then expose local `/health`.
- Avoided evaluator-file changes and avoided request caps below benchmark concurrency.

Actual best accepted framework result: 695.26 tok/s versus 100.64 tok/s baseline, or 6.91x faster.

Why it improved: the biggest win came from fixing request admission and warm-backend determinism. Once requests reached one warm vLLM engine concurrently, continuous batching did the work; before that, much of the benchmark was paying bridge/cold-start/serialization cost.

## Constrained JSON

Hypothesis: constrained decoding should expose grammar-mask and sampler overhead. The largest wins should come from reducing per-token XGrammar bitmask staging/copy churn and using a faster sampling backend when compatible.

Expected: moderate speedups because the model still does real guided generation, but remove avoidable CPU/H2D overhead from the constrained path.

Changes that helped:

- Reused grammar bitmask staging buffers instead of reallocating every step.
- Replaced per-row GPU copies with bulk/span copies while preserving `indices` semantics for mixed rows.
- Installed compatible FlashInfer sampling in the Modal image and split image layers so cold framework accuracy did not rebuild the heavy vLLM/torch layer.
- Added an all-structured/all-row fast path that avoids passing dynamic-length `indices` into the xgrammar torch-compile path.

Actual best accepted framework result: 427.20 tok/s versus 310.05 tok/s baseline, or 1.38x faster. Judge measured 450.44 tok/s on the same final direction.

Why it improved: the loop removed repeated ATen copy/view churn and reduced dynamic grammar-mask compile overhead. FlashInfer helped sampling once the image layering issue was fixed, and the final no-indices all-row fast path avoided xgrammar recompiles for the all-guided benchmark shape.

## xgrammar FSM Warnings

The repeated xgrammar FSM warnings mean xgrammar was building or warning about finite-state-machine artifacts for the structured-output schema path. They are not automatically correctness failures.

For this workload, the warnings were acceptable because the constrained-decoding accuracy gate passed: generated outputs were schema-valid and preserved per-request sentinels. If those warnings coincided with schema failures, fallback decoding, or missing sentinels, then they would be evidence of an actual bug.

## Headroom Notes

The long-prompt profile showed direct generate CUDA around 120-130 ms while HTTP p99 latency was often 1-2 s. That points to more headroom in queueing, proxy/stream cadence, scheduler behavior, and cold lifecycle than in individual matmul kernels.

For different workloads, headroom changes:

- Long-prompt repeated-prefix workloads have large prefix-cache and warmup headroom.
- High-concurrency short-output workloads have large admission/continuous-batching/readiness headroom.
- Constrained decoding has specialized grammar-mask, sampler, and torch-compile-shape headroom.
- Internal vLLM paging rewrites might help some workloads, but these experiments suggest the first-order wins here were serving-path and constrained-decoding overheads rather than a wholesale paging rewrite.

## Final One-H100 Remeasurement

I reran the final comparison with resource routing fixed to one Modal H100 per measurement: one Modal app under test, `gpu="H100"`, `max_containers=1`, `buffer_containers=0`, and `tensor_parallel_size=1`. The client benchmark routed only to that app's one Modal web endpoint.

| Version | Long prompts | High concurrency | Constrained JSON |
| --- | ---: | ---: | ---: |
| Stock vLLM, no FlashInfer sampler | 378.43 tok/s | 276.31 tok/s | 444.41 valid tok/s |
| Stock vLLM, FlashInfer sampler | 267.25 tok/s | 371.13 tok/s | 251.12 valid tok/s |
| Stock vLLM, FlashInfer installed but sampler disabled | not rerun | not rerun | 425.44 valid tok/s |
| Long-specialized | 238.84 tok/s | 472.77 tok/s | 0 valid tok/s |
| High-specialized | 369.60 tok/s | 675.39 tok/s | 0 valid tok/s |
| Constrained-specialized | max-model-len failure | 197.90 tok/s | 729.12 valid tok/s |

The high-concurrency specialization remains the largest fair target-pair gain: 675.39 tok/s versus 276.31 tok/s, or 2.44x over stock vLLM without FlashInfer. If the FlashInfer-enabled stock baseline is the comparison point for high concurrency, the gain is 1.82x.

The constrained-specialized version reached 729.12 valid tok/s. That is 1.64x faster than the stock no-FlashInfer constrained baseline and 2.90x faster than the stock FlashInfer-sampler constrained baseline. Because FlashInfer hurt stock constrained serving in this measurement, I treat both comparisons as useful: no-FlashInfer is the fairer stock baseline, while FlashInfer-sampler shows the failure mode that the constrained-specialized path avoided.

The table above is retained as the first fixed-resource measurement pass, but the long-prompt, high-concurrency, and constrained sections below supersede it. The later harnesses fixed benchmark bugs around streaming chunk counts, token accounting, prompt tokenization, failure handling, and resource-class gating.

## FlashInfer Sampler Profiling

Observation: enabling `VLLM_USE_FLASHINFER_SAMPLER=1` helped the high-concurrency workload but hurt stock constrained JSON serving:

| Stock constrained run | Valid tok/s | Completed/schema-ok | Total tokens | Mean TTFT | Mean TPOT | p99 latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| No FlashInfer sampler | 444.41 | 218/218 | 9,085 | 773.3 ms | 17.45 ms | 6,780.5 ms |
| FlashInfer sampler enabled | 251.12 | 171/171 | 5,169 | 1,094.3 ms | 31.30 ms | 7,344.9 ms |
| FlashInfer installed, sampler disabled | 425.44 | 202/202 | 8,944 | 1,011.6 ms | 15.03 ms | 5,784.7 ms |

Hypothesis before profiling: either the FlashInfer sampler kernel was slower for the constrained decoding shape, or enabling it changed the serving path enough to interact badly with xgrammar's dynamic bitmask application.

What I profiled: a stock vLLM `LLM.generate` constrained-JSON batch loop on one Modal H100, same model, same `max_num_batched_tokens=2048`, same CUDA graph capture sizes, FlashInfer installed in both cases, and only `VLLM_USE_FLASHINFER_SAMPLER` toggled before importing vLLM.

Profile result:

| Direct profile | Wall time for 8 batches | Total CPU time | Total CUDA time |
| --- | ---: | ---: | ---: |
| Sampler disabled | 58.12 s | 7.02 s | 11.10 s |
| FlashInfer sampler enabled | 58.64 s | 6.94 s | 11.07 s |

The direct profile did not reproduce the large serving benchmark regression. Attention and matmul kernels were effectively unchanged. The largest extra self-CPU item with FlashInfer enabled was `aten::view`, about +0.48 s across 8 profiled batches; that is real overhead, but it is too small to explain the 444 -> 251 valid tok/s drop by itself. The CUDA-side delta was dominated by one noisy profiled iteration that included TorchDynamo/xgrammar compile activity rather than a steady sampler kernel difference.

The isolation benchmark is more decisive: installing FlashInfer while disabling the sampler returned constrained throughput to 425.44 valid tok/s, close to the original 444.41 valid tok/s. That means the regression is not from the package being present or from a different image layer. It is specifically triggered by the FlashInfer sampler path under the async serving benchmark.

Current explanation: in the constrained workload, the sampler is not operating on ordinary unconstrained logits. Every step also applies xgrammar token masks, with dynamic `indices` lengths that hit TorchDynamo recompilation limits in the stock path. The PyTorch-native sampler appears to compose better with that masked-logits path in vLLM 0.10.0. FlashInfer is faster for the many-short-output unconstrained workload, but in constrained JSON serving its integration adds enough per-step shape/view/launch overhead, and possibly less favorable async scheduling cadence, that fewer requests complete in the 20 s closed-loop window. The best specialized constrained version improved by avoiding the dynamic `indices` path for the all-guided workload and by reducing grammar bitmask staging/copy churn; that removes the pressure point that made the stock FlashInfer run fragile.

## Constrained JSON Ablation

I ran one-H100 ablations to separate three factors in the constrained-specialized result:

- **Code patch**: local vLLM changes in `vllm/vllm/v1/worker/gpu_model_runner.py` that reuse grammar bitmask staging buffers, copy contiguous bitmask spans, and skip dynamic xgrammar `indices` when all logit rows are structured.
- **Tuned params**: `max_model_len=4096`, `max_num_seqs=64`, `max_num_batched_tokens=8192`, explicit chunked prefill/prefix caching, and CUDA graph sizes `[1, 2, 4, 8, 16, 32, 64]`.
- **FlashInfer sampler**: `VLLM_USE_FLASHINFER_SAMPLER=1` with `flashinfer-python==0.2.6.post1+cu128torch2.7`.

All runs used the same constrained JSON benchmark: closed-loop concurrency 16, 20 s duration, streaming `/v1/completions`, `max_tokens=96`, and throughput counted only schema-valid responses.

| Code patch | Tuned params | FlashInfer sampler | Valid tok/s | Completed/schema-ok | Mean TTFT | Mean TPOT | p99 latency |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| no | no | no | 444.41 | 218/218 | 773.3 ms | 17.45 ms | 6,780.5 ms |
| no | no | yes | 251.12 | 171/171 | 1,094.3 ms | 31.30 ms | 7,344.9 ms |
| no | yes | no | 243.24 | 189/189 | 902.3 ms | 40.68 ms | 7,698.9 ms |
| no | yes | yes | 331.93 | 164/164 | 1,211.1 ms | 24.35 ms | 8,135.0 ms |
| yes | no | no | 641.25 | 309/309 | 628.0 ms | 10.53 ms | 2,677.2 ms |
| yes | no | yes | 607.59 | 286/286 | 729.6 ms | 9.75 ms | 2,657.1 ms |
| yes | yes | no | 458.96 | 300/300 | 607.6 ms | 17.31 ms | 2,746.8 ms |
| yes | yes | yes | 729.12 | 364/364 | 481.3 ms | 10.98 ms | 2,446.4 ms |

Attribution:

- The core vLLM code patch is the dominant standalone improvement. Holding params and FlashInfer off, it improves constrained throughput from 444.41 to 641.25 valid tok/s, or 1.44x.
- Parameter tuning alone does not explain the improvement. With stock vLLM code, the tuned-params row regressed to 243.24 valid tok/s without FlashInfer and 331.93 valid tok/s with FlashInfer. Both stock-code tuned-param runs still showed the xgrammar dynamic-`indices` recompilation warning.
- FlashInfer alone also does not explain the improvement. On stock params and stock code it regressed to 251.12 valid tok/s. With the code patch and baseline-like params it was close to code-only but slightly lower, 607.59 valid tok/s versus 641.25.
- The best measured result is an interaction: code patch + tuned params + FlashInfer reached 729.12 valid tok/s. The tuned params are not independently good, but in the final patched path they likely change batch/graph/scheduler shape enough for FlashInfer and the no-indices grammar path to compose well.

The practical conclusion is that the constrained-specialized win should be credited primarily to implementation changes in vLLM's guided-decoding bitmask path. Parameter tuning is secondary and shape-sensitive; it was harmful without the code patch and only contributed to the best result in combination with the patched grammar path and FlashInfer sampler.

## High Concurrency Ablation

I ran the same one-H100 ablation style for the high-concurrency specialization, because it was the other workload with a clear final-measurement speedup.

The high-concurrency workload uses a 20 s closed-loop benchmark with many concurrent short `/v1/completions` requests. Throughput is aggregate output tokens per second.

Factors tested:

- **Baseline wrapper**: stock vLLM serving wrapper used for the one-H100 baseline.
- **Specialized wrapper**: optimized Modal wrapper from the high-concurrency run. It includes a local vLLM source tree in the image path, but I did not find a workload-specific internal vLLM code patch analogous to the constrained JSON grammar-mask patch.
- **GPU memory utilization**: `0.90` versus `0.92`, which changes available KV-cache capacity.
- **Worker multiprocessing method**: whether `VLLM_WORKER_MULTIPROC_METHOD=spawn` is set.
- **FlashInfer sampler**: stock vLLM with `VLLM_USE_FLASHINFER_SAMPLER=1`.

| Version | GPU memory utilization | Worker method override | FlashInfer sampler | Tok/s | Completed | Mean TTFT | Mean TPOT | p99 latency |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Stock baseline | 0.90 | spawn | no | 276.31 | 574/574 | 2,131.5 ms | 23.21 ms | 2,881.0 ms |
| Stock baseline | 0.90 | spawn | yes | 371.13 | 677/677 | 1,778.2 ms | 18.85 ms | 2,544.1 ms |
| Stock baseline | 0.90 | default | no | 690.99 | 1,054/1,054 | 1,133.3 ms | 12.54 ms | 2,185.1 ms |
| Stock baseline | 0.92 | spawn | no | 627.10 | 993/993 | 1,192.7 ms | 13.66 ms | 2,573.5 ms |
| High-specialized | 0.92 | default | no | 675.39 | 1,005/1,005 | 1,165.8 ms | 11.78 ms | 2,594.1 ms |
| High-specialized | 0.92 | spawn | no | 759.08 | 1,151/1,151 | 1,015.9 ms | 13.05 ms | 2,312.0 ms |
| High-specialized | 0.90 | default | no | 339.52 | 647/647 | 1,886.7 ms | 20.16 ms | 2,671.5 ms |

Follow-up audit:

The initial attribution above was too strong. The high-concurrency benchmark counts one output token for every non-empty SSE chunk:

```python
if text:
    output_tokens += 1
```

That is a weak metric for this workload because vLLM can change streaming chunk cadence without changing the actual generated content. The prompt pool is also 64 copies of the exact same prompt and `temperature=0`, so each successful request should generate the same continuation.

I reran an audit benchmark with the same closed-loop concurrency but counted requests, SSE chunks, and output characters separately. Every successful request produced exactly 64 output characters in these audit runs, confirming that chunk count was not the same as generated content length.

| Version | GPU memory utilization | Worker method override | Chunk/s | Char/s | Request/s | Chunks/request | Chars/request |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Stock baseline audit | 0.90 | spawn | 379.98 | 2,040.72 | 31.89 | 11.92 | 64.00 |
| Stock baseline audit | 0.92 | spawn | 572.90 | 2,693.92 | 42.09 | 13.61 | 64.00 |
| Stock baseline audit | 0.90 | default | 213.44 | 1,448.73 | 22.64 | 9.43 | 64.00 |
| High-specialized audit | 0.92 | default | 500.33 | 2,385.68 | 37.28 | 13.42 | 64.00 |

Revised attribution:

- The high-concurrency result is not explained by an internal vLLM code change. The specialized bundle carries vLLM source, but I did not find a workload-specific vLLM internals patch analogous to the constrained JSON grammar-mask patch.
- The apparent `0.90 -> 0.92` gain in the original table was overstated. On the corrected audit, stock `0.92` was still faster than stock `0.90`, but by about 1.32x in characters/sec and 1.32x in requests/sec, not 3x.
- The original stock `0.90` baseline was unstable. A rerun of the same stock `0.90 + spawn` setup measured 379.98 chunk/s instead of the earlier 276.31 chunk/s.
- The old token/s metric is confounded by streaming chunk coalescing. In the corrected audit, `chars/request` was fixed at 64.00 for all rows, while `chunks/request` ranged from 9.43 to 13.61.
- `spawn` is not independently explained by the existing measurements. The stock no-spawn row was fast in the original chunk-count benchmark but slow in the corrected rerun, so it is likely interacting with run-to-run scheduling/container variance.
- FlashInfer still appears helpful for this unconstrained short-output workload in the original table, but I have not yet rerun FlashInfer under the corrected character-count audit.

The practical conclusion is now narrower: the high-concurrency specialization should not be credited with a proven large vLLM performance improvement. The previous numbers show that the serving path is sensitive to runtime settings and Modal/vLLM scheduling state, but the benchmark is not engineered well enough to attribute a clean optimization win. The next proper benchmark should count true generated tokens, run multiple repetitions per config, and use either non-streaming responses with `usage.completion_tokens` or a streaming wrapper that reports token counts from vLLM output token ids.

## Long Prompts Robust Remeasurement

I reran the long-prompts/short-outputs workload after fixing the same benchmark issues as the high-concurrency and constrained reruns:

- Switched to non-streaming `/v1/completions` and counted `usage.completion_tokens` from vLLM output token ids.
- Requested fixed-length completions with `min_tokens=max_tokens=16`, `ignore_eos=True`, and `temperature=0`.
- Used concurrency 16, a prompt pool of 64 prompts, a 20 s warmup, and five 60 s fixed measurement windows.
- Scored throughput over the fixed trial duration and reported tail completions separately.
- Added explicit HTTP connection limits equal to concurrency.
- Added per-trial health checks, per-response `X-Benchmark-Instance`, and invalidated runs with instance changes, token-count mismatches, failed requests, or the wrong H100 memory class.
- Fixed the prompt generator. The first robust attempt used artificial strings like `p000w000`; Llama tokenized those into about 14,001 tokens, exceeding `max_model_len=8192`. The accepted benchmark now uses a shared natural-word 3000-word prefix plus a small per-request suffix.
- Fixed the long-specialized FlashInfer path by installing `flashinfer-python==0.2.6.post1+cu128torch2.7` into the Modal image and setting `VLLM_WORKER_MULTIPROC_METHOD=spawn` so CUDA is not initialized through forked worker processes.

Workload implementation:

- Benchmark: `.codex/final-one-h100-measurement/long_robust_tokens_benchmark.py`
- Stock wrapper: `.codex/final-one-h100-measurement/robust-high-baseline90`
- Specialized wrapper: `.codex/final-one-h100-measurement/worktrees/long-specialized/workspace`

Resource note: both accepted runs used one Modal `H100` app container, tensor parallel size 1, `NVIDIA H100 80GB HBM3`, and 79.18 GiB visible CUDA memory. They did not use the same physical GPU UUID. Stock used `GPU-7cec38f4-5f2e-c2da-71bf-78acca9d70fb`; specialized used `GPU-d3323cea-2720-7856-5906-22a0f5342697`.

Rejected attempts:

- Artificial-token prompt run: invalid because prompts tokenized longer than `max_model_len=8192`.
- First stock natural-prompt run: diagnostic only; median 640.80 tok/s but one trial had 11 HTTP 408s, so it failed the robust validity gate.
- First FlashInfer-fixed specialized attempt: rejected by resource guard because it landed on `NVIDIA H100 NVL`, not H100 80GB.
- First FlashInfer-enabled specialized startup: failed with CUDA fork reinitialization; fixed by forcing `spawn`.

| Version | FlashInfer sampler | Median tok/s | Mean tok/s | Stddev | Min | Max | Median req/s | Validity |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Stock vLLM 0.10.0 | no | 508.53 | 511.15 | 4.20 | 508.00 | 517.07 | 31.78 | valid |
| Long-specialized | yes | 512.53 | 509.49 | 6.62 | 499.47 | 515.20 | 32.03 | valid |

Robust speedup: `512.53 / 508.53 = 1.01x` by median true completion tokens/sec.

Interpretation: this is a tie, not a meaningful long-prompt specialization win. Fixing FlashInfer was the correct engineering move because the specialized wrapper intended to benchmark with `VLLM_USE_FLASHINFER_SAMPLER=1`, and the accepted run confirmed that path was active. But this workload emits only 16 output tokens after long repeated prompts, so the measured path is dominated by prefill, prefix-cache behavior, scheduler cadence, and request admission. FlashInfer affects sampling during decode, which is a small fraction of this workload. The long-specialized engine params, including `max_num_batched_tokens=4096` and small CUDA graph capture sizes, did not produce a stable gain over stock in the corrected harness.

Raw result files:

- `.codex/final-one-h100-measurement/results/long-robust-baseline90-rerun.json`
- `.codex/final-one-h100-measurement/results/long-robust-baseline90-rerun.meta.json`
- `.codex/final-one-h100-measurement/results/long-robust-specialized.json`
- `.codex/final-one-h100-measurement/results/long-robust-specialized.meta.json`

## High Concurrency Robust Remeasurement

After the streaming-chunk audit, I fixed the measurement harness before rerunning stock versus high-specialized:

- Switched the high-concurrency rerun to non-streaming `/v1/completions` and counted `usage.completion_tokens` backed by vLLM output token ids.
- Requested fixed-length completions with `min_tokens=max_tokens=16` and `ignore_eos=True`.
- Used five 60 s fixed measurement windows after a 20 s warmup.
- Scored throughput over the fixed window, not over window plus in-flight drain time.
- Reported tail completions separately; each trial had 64 tail requests, as expected for concurrency 64.
- Set explicit HTTP connection limits equal to concurrency.
- Added per-trial health checks, per-response `X-Benchmark-Instance`, and invalidated runs with instance changes, token-count mismatches, or failed requests.
- Logged CUDA device name/memory, `nvidia-smi` GPU UUID, worker process mode, FlashInfer sampler setting, and vLLM config knobs.
- Pinned both stock and specialized to `VLLM_WORKER_MULTIPROC_METHOD=spawn`, `VLLM_USE_FLASHINFER_SAMPLER=0`, `gpu_memory_utilization=0.92`, one Modal app container, and tensor parallel size 1.
- Changed the specialized non-streaming wrapper from `RequestOutputKind.CUMULATIVE` to `FINAL_ONLY` so the benchmark path matches stock.

Important resource note: the stock and specialized reruns both used the same Modal resource envelope, `NVIDIA H100 80GB HBM3`, 79.18 GiB visible CUDA memory, and 57.16 GiB available KV cache. They did not use the same physical GPU UUID, so this is a same-GPU-class comparison, not a same-card comparison.

| Version | Median tok/s | Mean tok/s | Stddev | Min | Max | Median req/s | Validity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Stock vLLM 0.10.0 | 1,035.73 | 1,039.68 | 16.48 | 1,023.73 | 1,060.27 | 64.73 | valid |
| High-specialized | 1,147.47 | 1,111.41 | 86.19 | 1,008.27 | 1,199.47 | 71.72 | valid |

Robust speedup: `1,147.47 / 1,035.73 = 1.11x` by median true completion tokens/sec.

This replaces the earlier 2.44x/6.91x high-concurrency claims. The old measurements were confounded by streaming chunk counts, possible GPU-memory-class differences, and benchmark drain accounting. The remaining measured gain is modest and noisy; the first two specialized trials were near or below stock, while the last three were faster. I would treat this as evidence of at most a small serving-path gain unless repeated runs reproduce the same median with lower variance.

## Constrained JSON Robust Remeasurement

I reran the constrained-decoding workload with the same measurement fixes used for the high-concurrency rerun:

- Switched from streaming SSE chunk counts to non-streaming `/v1/completions` and counted `usage.completion_tokens` from vLLM output token ids.
- Kept the constrained workload shape: concurrency 16, `max_tokens=96`, `temperature=0`, and the same five profile prompts plus the same JSON schema.
- Validated every counted response against the JSON schema.
- Used a 20 s warmup followed by five 60 s fixed measurement windows.
- Scored throughput over the fixed trial duration, not over drain time.
- Added explicit HTTP connection limits equal to concurrency.
- Added `/health` metadata and `X-Benchmark-Instance` response headers to ensure requests stayed on one Modal app container.
- Guarded the specialized run to the same H100 memory class reported by stock: `NVIDIA H100 80GB HBM3`, 79.18 GiB visible CUDA memory, tensor parallel size 1, `max_containers=1`, `buffer_containers=0`.

Workload implementation:

- Benchmark: `.codex/final-one-h100-measurement/constrained_robust_tokens_benchmark.py`
- Stock wrapper: `.codex/final-one-h100-measurement/robust-constrained-baseline90`
- Specialized wrapper: `.codex/final-one-h100-measurement/robust-constrained-specialized`

Resource note: both runs used the same Modal H100 resource envelope and the same visible CUDA memory size, but not the same physical GPU UUID. Stock used `GPU-e6b8157b-1412-b603-5bec-e41f46d345eb`; specialized used `GPU-fa87039a-c176-63ad-cf0d-b60f1544b661`.

The stock run hit the same xgrammar dynamic-`indices` recompilation warning seen earlier:

```text
torch._dynamo hit config.recompile_limit ... apply_token_bitmask_inplace_kernel_indices_torch_compile ... len(indices)
```

That warning did not correspond to incorrect output in this measurement. All completed stock and specialized responses passed schema validation. The stock run had one transient `502 Bad Gateway` transport error in trial 2; throughput excludes that failed request. The first benchmark version treated any transport error as fatal, so the stock JSON's `valid` flag is `false`, but the correctness counters were clean: zero schema failures, stable instance, and one excluded request error across five trials.

| Version | Median tok/s | Mean tok/s | Stddev | Min | Max | Median req/s | Schema failures | Transport failures | Validity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Stock vLLM 0.10.0, no FlashInfer sampler | 934.33 | 929.89 | 11.51 | 915.38 | 943.27 | 19.63 | 0 | 1 | usable with transport caveat |
| Constrained-specialized, code + params + FlashInfer | 1,227.12 | 1,221.93 | 13.96 | 1,197.15 | 1,230.35 | 25.78 | 0 | 0 | valid |

Robust speedup: `1,227.12 / 934.33 = 1.31x` by median true completion tokens/sec.

This replaces the earlier constrained JSON claim of 1.64x versus stock no-FlashInfer and 2.90x versus stock FlashInfer-sampler. The old result was inflated by the streaming benchmark's chunk-count metric and short 20 s window. The corrected result still supports a real constrained-specialized win, but it is closer to 31% than to 64-190%.

Why it improved: this is the workload where the specialized vLLM internals patch is actually relevant. The stock run still exercises xgrammar's dynamic `indices` masked-logits path and triggers recompilation warnings. The specialized source includes changes in the guided-decoding bitmask path that reuse grammar bitmask staging buffers, copy contiguous bitmask spans, and skip the dynamic `indices` path when all logit rows are structured. The wrapper also changes engine params and enables FlashInfer sampling, so I ran the robust ablation below to split those effects apart. That ablation shows the guided-decoding code patch is the dominant standalone explanation, and that the best measured combination is patched code plus FlashInfer with stock-like engine params.

Raw result files:

- `.codex/final-one-h100-measurement/results/constrained-robust-baseline90.json`
- `.codex/final-one-h100-measurement/results/constrained-robust-baseline90.meta.json`
- `.codex/final-one-h100-measurement/results/constrained-robust-specialized.json`
- `.codex/final-one-h100-measurement/results/constrained-robust-specialized.meta.json`

## Constrained JSON Robust Ablation

I reran the constrained-decoding ablation with the corrected benchmark harness from the previous section. This supersedes the earlier 20 s streaming ablation table, which counted SSE chunks instead of true completion tokens.

All accepted rows used one Modal `H100` app container, tensor parallel size 1, `NVIDIA H100 80GB HBM3`, and 79.18 GiB visible CUDA memory. Attempts that landed on `NVIDIA H100 NVL` with 93.09 GiB visible memory were rejected by the measurement guard before benchmark execution and are not included below.

Definitions:

- **Code patch**: local vLLM guided-decoding changes that reuse grammar bitmask staging buffers, copy contiguous bitmask spans, and skip xgrammar's dynamic `indices` path when all logit rows are structured.
- **Tuned params**: `max_model_len=4096`, `max_num_seqs=64`, `max_num_batched_tokens=8192`, explicit small CUDA graph capture sizes, chunked prefill enabled, and prefix caching enabled.
- **FlashInfer**: `flashinfer-python==0.2.6.post1+cu128torch2.7` with `VLLM_USE_FLASHINFER_SAMPLER=1`.

| Row | Code patch | Tuned params | FlashInfer sampler | Median tok/s | Mean tok/s | Stddev | Speedup vs stock | Median req/s | Failures | Validity |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Stock baseline | no | no | no | 934.33 | 929.89 | 11.51 | 1.00x | 19.63 | 1 transport, 0 schema | usable with transport caveat |
| Params only | no | yes | no | 1,257.13 | 1,249.56 | 12.48 | 1.35x | 26.42 | 0 | valid |
| Params + FlashInfer | no | yes | yes | 1,233.08 | 1,231.40 | 11.15 | 1.32x | 25.90 | 0 | valid |
| Code only | yes | no | no | 1,311.27 | 1,303.39 | 27.36 | 1.40x | 27.55 | 0 | valid |
| Code + FlashInfer | yes | no | yes | 1,353.18 | 1,347.95 | 10.76 | 1.45x | 28.43 | 0 | valid |
| Code + params, no FlashInfer | yes | yes | no | 1,203.35 | 1,177.94 | 85.02 | 1.29x | 25.28 | 1 transport, 0 schema | valid |
| Full earlier specialized | yes | yes | yes | 1,227.12 | 1,221.93 | 13.96 | 1.31x | 25.78 | 0 | valid |

Interpretation:

- The best robust row is **code patch + FlashInfer with stock-like params**, at 1,353.18 tok/s, or 1.45x over the stock baseline.
- The vLLM code patch is still the cleanest standalone explanation: `code only` beats stock by 1.40x and beats `params only` by about 4%.
- The parameter changes do help stock vLLM under this robust benchmark, unlike the older streaming ablation. However, they do not compose well with the patched code in this workload. `code + params, no FlashInfer` drops to 1,203.35 tok/s and has high trial variance, and the full earlier specialized row remains below `code only`.
- FlashInfer is shape-sensitive here. With stock code plus tuned params it is slightly slower than params-only, but with the code patch and stock-like params it gives the best result. My hypothesis is that removing the dynamic grammar-mask `indices` path makes the sampler path less fragile, while the tuned params shift batching/graph shapes in a way that is not favorable for this constrained workload.
- The earlier conclusion that the full specialized wrapper's params were helpful in combination is no longer supported by the robust ablation. The safer current conclusion is: **keep the guided-decoding code patch, keep FlashInfer for the patched stock-like constrained row, and retune engine params from scratch rather than carrying forward the previous full-specialized values**.

Why stock vLLM has the more general implementation:

Stock vLLM has to handle mixed serving batches. A single logits batch can contain unconstrained chat requests next to JSON-schema requests, and the structured-output requests can appear at arbitrary row positions. The scheduler provides a compact grammar bitmask only for the structured requests, so the general path has to map request ids to current logits rows, build a full sorted bitmask aligned to the logits tensor, track `indices` for the rows that actually need masking, copy the bitmask to GPU, and call xgrammar with those `indices`. That is the right shape for correctness across arbitrary mixed constrained/unconstrained batches.

The constrained benchmark has a narrower shape: every active request is a structured JSON request, and the relevant logits rows are usually one contiguous structured span. Under that workload specialization, the `indices` list is redundant because every row needs masking. The patch removes overhead by reusing CPU/GPU grammar-bitmask staging buffers, copying contiguous spans instead of rebuilding and moving a fresh full bitmask each step, and calling xgrammar without dynamic `indices` in the all-rows-structured case. This matters because grammar masking is on the per-token decode path; small Python allocation, sorting, copy, and TorchDynamo shape costs repeat once per generated token across the whole concurrent batch.

Raw ablation result files:

- `.codex/final-one-h100-measurement/results/constrained-robust-ablate-params-only.json`
- `.codex/final-one-h100-measurement/results/constrained-robust-ablate-params-fi.json`
- `.codex/final-one-h100-measurement/results/constrained-robust-ablate-code-only.json`
- `.codex/final-one-h100-measurement/results/constrained-robust-ablate-code-fi.json`
- `.codex/final-one-h100-measurement/results/constrained-robust-ablate-code-params-no-fi.json`
