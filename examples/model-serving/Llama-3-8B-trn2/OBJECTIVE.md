# Objective — Llama-3-8B inference server on AWS Trainium (trn2)

Maximize **output token throughput (tok/s)** for `Meta-Llama-3-8B-Instruct`
on a single AWS Trainium2 device while keeping accuracy within the accuracy
checker's tolerance. Build an OpenAI-compatible `/v1/chat/completions` and
`/v1/completions` server. Model weights and tokenizer are provided locally at
`reference/model` (mounted read-only in the container).

Headline metric: `aggregate_throughput` (the peak Request Factory output-token
throughput across the declared matrix). Report this exact field as `perf_metric`
every round; do not substitute or invert it.

## Hardware

- A single `trn2.3xlarge`: **1 Neuron device, 4 NeuronCores, ~96 GB device
  memory.** The device is exposed as `/dev/neuron0` inside the container.
- This is **not** a GPU. There is no CUDA, no `nvidia-smi`, no `torch.cuda`.
  The accelerator is programmed through the AWS Neuron SDK.

## Implementation rules

- Build the model in **PyTorch** with explicit layers (your own attention / MLP
  / RMSNorm / RoPE), using `transformers` only as a utility for config /
  tokenizer / weight loading. Keep the architecture yours — don't hide the whole
  model behind a one-call turnkey `generate()`.
- You **may** (and for performance, should) build on **NxD Inference**
  (`neuronx_distributed` / `neuronx_distributed_inference`) for the
  Neuron-specific serving plumbing — in particular its **`KVCacheManager`**
  (device-resident, in-place KV cache) and **`ModelBuilder`** (tracing + the
  input/output aliasing that keeps the cache resident across decode steps). A
  from-scratch KV cache on raw `torch_neuronx.trace` cannot stay device-resident
  — NxD is the supported way to get there. See the **`nxd-kv-cache`** skill.
- Run it on the NeuronCore in **BF16**; do not run the hot path on CPU or in
  `float32` (a server that loads but silently falls back to CPU is incorrect for
  this target). See the Trainium skill for how PyTorch maps onto NeuronCores.
- Quantization, batching, KV-cache layout, and using more than one of the 4
  NeuronCores are optimizations, not prerequisites.

## Notes

- Text-generation, dense causal LM (Llama-3-8B: 32 layers, hidden 4096, GQA
  with 8 KV heads, vocab 128256, RoPE theta 5e5, 8192-token context, no
  rope-scaling).
- Request Factory drives `/v1/completions` with input/output lengths
  (128/256/512), 64 requests per point, and concurrency levels (1/2/4/8). It
  uses saturated arrival mode bounded by each matrix point's concurrency. This
  fixed-volume methodology replaces the former duration-driven bundle client;
  compare its scores accordingly. `aggregate_throughput` is the maximum RF
  output-token throughput across the full matrix, and every matrix point is
  retained in the raw benchmark result.
- Correctness is judged by the accuracy checker against the HuggingFace
  Transformers reference; match its tolerance before chasing throughput.
