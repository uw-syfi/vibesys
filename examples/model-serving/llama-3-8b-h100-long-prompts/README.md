# Llama 3.1 8B vLLM H100 Long-Prompt Input

Use:

- `--input examples/model-serving/llama-3-8b-h100-long-prompts`
- `--interface service`
- `--modal` for H100-backed runs

This input materializes a pinned editable vLLM checkout into the candidate
workspace through `workspace.sources`, then benchmarks repeated long prompts
with short outputs.
