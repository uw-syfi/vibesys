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

- Build the model yourself — explicit layers (your own attention / MLP /
  RMSNorm / RoPE), using `transformers` only as a utility for config /
  tokenizer / weight loading. Do **not** import a turnkey serving library (no
  `vllm`, no high-level `transformers-neuronx` / `optimum.neuron` model
  classes). The point is a bespoke implementation.
- Run it on the NeuronCore through the AWS Neuron SDK's PyTorch support. Choose
  an approach that is current and works for this hardware — consult the Neuron
  skills / docs rather than assuming a particular API. Use **BF16**; do not run
  the hot path on CPU or in `float32` (a server that loads but silently falls
  back to CPU is incorrect for this target).
- NeuronCores execute compiled graphs and recompiling is expensive, so keep
  shapes static (fixed prompt/decode buckets) and reuse the persistent compile
  cache.
- Quantization, batching, KV-cache layout, and using more than one of the 4
  NeuronCores are optimizations, not prerequisites.

## Notes

- Text-generation, dense causal LM (Llama-3-8B: 32 layers, hidden 4096, GQA
  with 8 KV heads, vocab 128256, RoPE theta 5e5, 8192-token context, no
  rope-scaling).
- The benchmark drives the server over HTTP with a fixed-length Poisson sweep
  (input == output length of 128/256/512 tokens, rates up to 2.0 req/s) and
  reports `aggregate_throughput` (tok/s) — the primary metric.
- Correctness is judged by the accuracy checker against the HuggingFace
  Transformers reference; match its tolerance before chasing throughput.
