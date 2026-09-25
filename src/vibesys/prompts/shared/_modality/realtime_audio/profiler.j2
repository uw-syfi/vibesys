## Server Type: Realtime Audio (WebSocket)

The server exposes an OpenAI-compatible Realtime API:
- `WebSocket /v1/realtime` — bidirectional audio/text streaming
- `POST /v1/realtime/sessions` — session creation
- `GET /health` — health check

Key performance axes: **audio-to-audio latency** (time from user speech end to first response audio), **streaming throughput**, **concurrent session scaling**. The audio encoder, decoder, and vocoder run in a tight loop; look for GPU idle gaps between components.

## Profiling Commands

**Warmup** (run after server health check, before and during profiling):
```
curl -s -X POST http://localhost:8077/v1/realtime/sessions -H "Content-Type: application/json" -d '{}' --max-time 30
```

**Workload** (run during profiling when no benchmark tool is available):
```
python -c "
import asyncio, websockets, json, base64
async def test():
    async with websockets.connect('ws://localhost:8077/v1/realtime') as ws:
        await ws.send(json.dumps({'type':'response.create'}))
        for _ in range(10):
            msg = json.loads(await ws.recv())
            if msg.get('type') == 'response.done': break
asyncio.run(test())
"
```
