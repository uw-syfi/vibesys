Llama 3.1 8B Instruct MLX 8-bit input bundle.

Use:
- `--ref inputs/Llama-3.1-8B-Instruct-MLX-8bit/reference`
- `--acc-checker inputs/Llama-3.1-8B-Instruct-MLX-8bit/accuracy_checker`
- `--bench inputs/Llama-3.1-8B-Instruct-MLX-8bit/benchmark`

This bundle targets `mlx-community/Meta-Llama-3.1-8B-Instruct-8bit`,
the MLX 8-bit quantized Llama 3.1 8B Instruct model used as the target
verifier in the speculative decoding playground.

The reference folder keeps model weights out of git. On first real use,
`reference/reference.py` downloads the pinned Hugging Face snapshot into the
repo `.hf_cache` and links it as `reference/model`.

The server contract matches the JSON-generation text bundles:
- `/health` returns 200 when the server is ready.
- `/v1/completions` accepts `prompt`, `max_tokens`, `temperature`, and
  `stream`, plus OpenAI-style `response_format: {"type": "json_schema", ...}`.
- Streaming responses use Server-Sent Events with OpenAI-style completion
  chunks whose `choices[0].text` field contains non-empty token deltas.

The checker and benchmark load the full `epfl-dlab/JSONSchemaBench` dataset
through Hugging Face `datasets`, pinned to revision
`5bd0f4640badc6f3f02df796421d21cb0ca0b141`. Use `--dataset-subset`,
`--split`, `--limit`, and `--dataset-cache-dir` to control the run.

This is the target model bundle only. The speculative draft model from the
playground is `mlx-community/Llama-3.2-1B-Instruct-4bit` and is configured
separately with server flags such as `--draft-model-dir`.
