# Objective - Show-o2 1.5B HQ image generation server on MacBook

Minimize **p50 text-to-image request latency** on a local Apple Silicon MacBook
for `showlab/show-o2-1.5B-HQ` while preserving the image-generation server
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

## MacBook implementation notes

- Show-o2 is a native unified multimodal model, not a conventional diffusion
  pipeline bolted onto a separate text encoder. The target uses a Qwen2.5-style
  Transformer body, a text head, a flow-matching image head, and a Wan VAE for
  image decode.
- The reference config uses hidden size 1536, 10 diffusion layers, 27 x 27 image
  latents with 16 channels, and the `showlab/show-o2-1.5B-HQ` checkpoint pinned
  in `reference/meta.json`.
- Use Apple Silicon-specific execution with MLX / Metal optimizations.
- Optimize the fixed text-to-image path first: body forward, diffusion-head
  steps, VAE decode, image postprocessing, and response encoding.
- Useful directions from the paper setup include porting the Qwen2.5-style body
  and 10-block diffusion head to MLX, eliding redundant SigLIP work on noisy
  latents, adding prefix-KV caches across diffusion steps, trimming prefill work,
  and using native postprocess paths.
- Cross-step redundancy is the main algorithmic opportunity on this target.
  Cache reusable body and head state across diffusion steps when the request
  shape is fixed.
- Treat quantization as target-specific tuning. Quantizing the compute-bound
  body may regress latency, while int4 can help bandwidth-bound head work if
  output quality remains acceptable.
- Classifier-free-guidance stride is a useful target-specific optimization:
  skip the unconditional branch on most steps and reuse the cached unconditional
  velocity when doing so preserves image quality for the benchmark settings.
- Precision, operation order, and postprocess changes may alter exact PNG bytes.
  Treat small image drift as acceptable only when request settings and output
  resolution stay fixed and the output remains a real prompt-conditioned image.
