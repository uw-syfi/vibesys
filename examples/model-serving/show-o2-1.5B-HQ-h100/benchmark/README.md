Benchmark inputs for Show-o2 1.5B HQ.

Run:

```bash
python benchmark.py \
  --url http://localhost:8000 \
  --warmup-requests 1 \
  --closed-loop \
  --collect-server-timings \
  --save-images-dir /tmp/show_o2_images \
  --postprocess-mode native \
  --prompt "a small red robot holding a handwritten sign that says VibeServe" \
  --request-seed 1234 \
  --fixed-request-seed \
  --num-requests 5 \
  --steps 4
```

The benchmark posts to `/v1/images/generations` and checks that each completed
request returns a base64 PNG payload. Warmup requests run before timed
measurement and are reported separately in JSON output. `--closed-loop`
measures one request at a time without queueing overlap, while
`--collect-server-timings` asks the server to return phase timings under
`server_timings`. `--postprocess-mode` can override the server image
postprocess path per request for comparison runs; supported modes are
`upstream`, `cpu`, and `native`. JSON output includes PNG SHA-256 hashes so
comparison runs can spot pixel-level drift across variants, plus PNG dimensions
so same-resolution comparisons can be checked explicitly. Pass
`--save-images-dir` to write completed warmup and measured PNG responses to
disk; measured image paths are included under `image_paths` in JSON output.

Use `compare_images.py` to quantify output drift between saved PNGs:

```bash
python compare_images.py \
  --baseline /tmp/show_o2_images_base/request_0000.png \
  --candidate /tmp/show_o2_images_opt/request_0000.png \
  --output-json /tmp/show_o2_compare.json
```

The comparison reports exact hash match, MAE, RMSE, PSNR, changed-pixel
fraction, and local luminance SSIM. Optional CLIP scoring is available with
`--clip-model` when the requested CLIP model is already available locally, or
with `--allow-clip-download` when downloads are acceptable.

When the server is started with `--vae-profile`, `--collect-server-timings`
also aggregates synchronized VAE decode sub-stage timings under
`vae_profile_*` keys. Use these for bottleneck diagnosis only; the extra
synchronization adds measurement overhead.

For performance optimization runs, keep the output shape fixed across variants:
use the same server resolution, prompt, request seed, inference step count,
guidance scale, and device profile. Precision/order changes may change final
pixel bytes; treat that as acceptable drift when the resolution and request
settings stay fixed.
