# Objective — Llama-3-8B inference server

Maximize **output token throughput (tok/s)** on a single H100 while keeping accuracy within the accuracy checker's tolerance. Build an OpenAI-compatible `/v1/chat/completions` and `/v1/completions` server.

## Notes

- Text-generation, dense causal LM. Hopper-class hardware assumed.
- Implement model layers explicitly (own attention / MLP / norm / RoPE); use
  `transformers` only as a utility for config / tokenizer / weight loading.
- FP16/BF16 is a reasonable baseline; quantization is an optimization, not a
  prerequisite.
- Benchmark harness drives the server; it reports req/s and tok/s. Prefer
  tok/s as the primary metric.
