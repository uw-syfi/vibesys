# Objective - Show-o2 1.5B HQ image generation server on H100

Minimize **p50 text-to-image request latency** on a single NVIDIA H100 for
`showlab/show-o2-1.5B-HQ` while preserving the image-generation server
contract. Build an OpenAI-like `/v1/images/generations` HTTP server that
returns prompt-conditioned PNG images.

## Workload

The benchmark posts image-generation requests with:

- `prompt`: either a fixed prompt or one prompt from the benchmark prompt pool.
- `num_inference_steps`: default 20, often fixed to a smaller value for smoke
  and comparison runs.
- `guidance_scale`: default 5.0.
- optional `seed` for deterministic requests.
- optional `include_timings` to return per-request timing diagnostics.
- optional `postprocess_mode` in `upstream`, `cpu`, or `native`.
- optional `response_format` in `b64_json`, `png`, or `ppm`.

Warmup requests run before the timed measurement and are excluded from the
headline result. For performance comparisons, keep prompt, seed, step count,
guidance scale, output resolution, response format, and postprocess mode fixed
across variants.

## Headline metric

Use `latency.p50` from `benchmark/benchmark.py`'s measured-result JSON as the
primary performance metric. Lower is better. `request_throughput`,
`server_timings`, and per-phase timing fields are secondary diagnostics.

## Server contract

- `/health` returns 200 when the server is ready.
- `/v1/images/generations` accepts the request fields above.
- The default response is OpenAI-style JSON with `data[0].b64_json` containing
  PNG bytes. If `response_format` is `png` or `ppm`, raw image bytes are
  allowed as implemented by the benchmark.
- When `include_timings` is true, return phase timings as `timings_ms` for JSON
  responses or `X-ShowO2-Timings-Ms` for raw-image responses.
- The accuracy checker is an HTTP smoke test that verifies a valid PNG. Do not
  exploit that by returning canned images; benchmark runs can save image hashes,
  dimensions, and images for drift inspection.

## H100 implementation notes

- Show-o2 is a native unified multimodal model, not a conventional diffusion
  pipeline bolted onto a separate text encoder. The target uses a Qwen2.5-style
  Transformer body, a text head, a flow-matching image head, and a Wan VAE for
  image decode.
- The reference config uses hidden size 1536, 10 diffusion layers, 27 x 27 image
  latents with 16 channels, and the `showlab/show-o2-1.5B-HQ` checkpoint pinned
  in `reference/meta.json`.
- Optimize the fixed text-to-image path first: body forward, diffusion-head
  steps, VAE decode, image postprocessing, and response encoding.
- Use CUDA-specific optimizations for the H100 target. Useful directions from
  the paper setup include CUDA graph replay and prewarm, reducing inactive
  diffusion-token work, restricting adaptive layer-norm work to active image
  spans, trimming unused Qwen tail work, and improving VAE/postprocess layout.
- Validate precision and kernel substitutions carefully. FlashAttention-2, GQA,
  `torch.compile`, and naive fp16 changes may alter outputs or produce NaNs on
  this workload; keep them only when the PNG contract and image quality remain
  acceptable.
- Precision, operation order, and postprocess changes may alter exact PNG bytes.
  Treat small image drift as acceptable only when request settings and output
  resolution stay fixed and the output remains a real prompt-conditioned image.
