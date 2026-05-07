## Server Type: Video Generation (Spatiotemporal Diffusion)

The server exposes OpenAI-compatible video generation endpoints:
- `POST /v1/videos` — async job submission
- `GET /v1/videos/{id}` — poll status
- `GET /health` — health check

Key performance axes: **total generation time**, **denoising step time**, **memory utilization** (video tensors are large). Temporal attention layers and VAE decode of multi-frame latents are typical hotspots.

## Profiling Commands

**Warmup** (run after server health check, before and during profiling):
```
curl -s -X POST http://localhost:8077/v1/videos -H "Content-Type: application/json" -d '{"prompt":"warmup","seconds":2}' --max-time 600
```

**Workload** (run during profiling when no benchmark tool is available):
```
curl -s -X POST http://localhost:8077/v1/videos -H "Content-Type: application/json" \
  -d '{"prompt":"A dog running in a park","seconds":4}'
# Poll until complete
VIDEO_ID=$(curl -s ... | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
for i in $(seq 1 120); do
  STATUS=$(curl -s http://localhost:8077/v1/videos/$VIDEO_ID | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "completed" ] && break; sleep 5
done
```
