# Llama-3.3 70B Dense 2xH100 Input

Use:

- `--input examples/model-serving/llama-70b-2xh100`
- `--runs-dir /work/vibesys-runs`
- `--local`
- `--interface service`
- `--modal` for H100-backed runs

The objective, checkers, and benchmark live in `.vibesys/tasks/default/`; the
single task is selected automatically, so no `--task` flag is needed.

This input targets `meta-llama/Llama-3.3-70B-Instruct` on two H100 GPUs. The
model does not fit in one GPU's memory, so the implementation must shard it
across both devices. The agent builds the server from scratch (no vLLM seed).
