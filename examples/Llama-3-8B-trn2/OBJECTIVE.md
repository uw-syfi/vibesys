# Objective — Llama-3-8B inference server on AWS Trainium (trn2)

Maximize **output token throughput (tok/s)** for `Meta-Llama-3-8B-Instruct`
on a single AWS Trainium2 device while keeping accuracy within the accuracy
checker's tolerance. Build an OpenAI-compatible `/v1/chat/completions` and
`/v1/completions` server. Model weights and tokenizer are provided locally at
`reference/model` (mounted read-only in the container).

Headline metric: `aggregate_throughput` (the output-token-throughput field, in
tok/s, of the benchmark tool's `--output-json` output). Report this exact field
as `perf_metric` every round; do not substitute or invert it.

## Hardware

- A single `trn2.3xlarge`: **1 Neuron device, 4 NeuronCores, ~96 GB device
  memory.** The device is exposed as `/dev/neuron0` inside the container.
- This is **not** a GPU. There is no CUDA, no `nvidia-smi`, no `torch.cuda`.
  The accelerator is programmed through the AWS Neuron SDK.

## Implementation rules

- Target the NeuronCore through **raw `torch-neuronx` / `torch_xla`**: build the
  model with explicit layers (your own attention / MLP / RMSNorm / RoPE), place
  tensors on the XLA device (`xm.xla_device()`), and compile the hot path with
  `torch_neuronx.trace` (or XLA lazy execution + `xm.mark_step()`). Use
  `transformers` only as a utility for config / tokenizer / weight loading — do
  **not** import a turnkey serving library (no `vllm`, no `transformers-neuronx`
  high-level model classes, no `optimum.neuron` pipelines). The point is a
  bespoke, from-scratch Neuron implementation.
- **BF16** is the baseline dtype. Do not run the hot path on CPU or in
  `float32` — a server that loads but silently falls back to CPU is incorrect
  for this target.
- NeuronCores compile static-shape graphs with `neuronx-cc`. The **first**
  compile of each new shape takes minutes; keep shapes static (fixed bucket
  sizes for prompt/decode), reuse the persistent compile cache
  (`NEURON_COMPILE_CACHE_URL`), and avoid shapes that force recompilation on the
  hot path.
- Quantization, sequence/continuous batching, KV-cache layout tuning, and core
  parallelism across the 4 NeuronCores are optimizations, not prerequisites.

## Notes

- Text-generation, dense causal LM (Llama-3-8B: 32 layers, hidden 4096, GQA
  with 8 KV heads, vocab 128256, RoPE theta 5e5, 8192-token context, no
  rope-scaling).
- The benchmark harness drives the server over HTTP and reports req/s and tok/s.
  Prefer `aggregate_throughput` (tok/s) as the primary metric.
- Correctness is judged by the accuracy checker against the HuggingFace
  Transformers reference; match its tolerance before chasing throughput.
