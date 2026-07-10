Optimize a Show-o2 1.5B HQ text-to-image server while preserving the
image-generation HTTP contract.

Headline metric: `latency.p50` from `benchmark/benchmark.py` (minimize).

Build an OpenAI-like `/v1/images/generations` service for
`showlab/show-o2-1.5B-HQ` that returns valid prompt-conditioned PNG images.
The server must expose `/health`, accept benchmark request fields such as
`prompt`, `num_inference_steps`, `guidance_scale`, `seed`, `include_timings`,
`postprocess_mode`, and `response_format`, and return either OpenAI-style JSON
with `data[0].b64_json` or raw image bytes when requested.

The accuracy checker verifies HTTP readiness and valid image responses. Do not
exploit the smoke test by returning canned images; benchmark runs may inspect
image hashes, dimensions, and saved outputs for drift.
