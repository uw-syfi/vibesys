## Server Type: Image Generation (Diffusion)

The server exposes an OpenAI-compatible image generation endpoint:
- `POST /v1/images/generations` — `{prompt, n, size, quality, response_format}`
- `GET /health` — health check

Key performance axes: **end-to-end latency** per image, **throughput** (images/min), **denoising step time**. The diffusion loop (many forward passes of the denoiser) dominates; look for kernel efficiency and scheduling overhead per step.

## Profiling Commands

**Warmup** (run after server health check, before and during profiling):
```
curl -s -X POST http://localhost:8077/v1/images/generations -H "Content-Type: application/json" -d '{"prompt":"warmup","n":1,"size":"256x256"}' --max-time 300
```

**Workload** (run during profiling when no benchmark tool is available):
```
for i in 1 2 3; do
  curl -s -X POST http://localhost:8077/v1/images/generations -H "Content-Type: application/json" \
    -d '{"prompt":"A cat sitting on a mat","n":1,"size":"1024x1024"}' -o /dev/null
done
```
