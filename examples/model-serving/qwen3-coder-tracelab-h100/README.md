# qwen3-coder-tracelab-h100

TraceLab-shaped vLLM serving target for `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` on Modal H100.

Use:

- `--ref examples/model-serving/qwen3-coder-tracelab-h100/reference`
- `--acc-checker examples/model-serving/qwen3-coder-tracelab-h100/accuracy_checker`
- `--bench examples/model-serving/qwen3-coder-tracelab-h100/benchmark`

The benchmark replays real TraceLab public coding-agent sessions through
TraceLab's own `session_runner` instead of independent chat requests. VibeSys
keeps the TraceLab submodule and released trace data in a hidden evaluator area,
outside the candidate workspace, so optimization agents can tune the serving
stack without inspecting or modifying the replay implementation.

Start an optimization run with:

```bash
./vs \
  --input examples/model-serving/qwen3-coder-tracelab-h100 \
  --exp-name qwen3-coder-tracelab-h100 \
  --modal \
  --modal-gpu H100 \
  --agent-backend cli \
  --cli-provider codex \
  --backend cuda \
  --interface service \
  --modality text_generation \
  --profiler torch \
  --max-rounds 4 \
  --headless
```
