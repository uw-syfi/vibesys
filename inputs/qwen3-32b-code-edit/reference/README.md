# Reference bundle — Qwen/Qwen3-32B

There is **no separate drafter**. The predicted-outputs path uses the request's `prediction.content` as its draft sequence — see the implementation sketch in `OBJECTIVE.md`. No EAGLE3 head, no Qwen3-0.6B drafter, no draft-model KV cache.

## Files in this directory

| File | Description |
|:--|:--|
| `config.json` | `Qwen/Qwen3-32B` verifier config. |
| `reference.py` | `transformers.models.qwen3.modeling_qwen3` reference module. |
| `meta.json` | model id + pinned commit revision. |

The model weights themselves are not in this directory — populate `model/` with a `Qwen/Qwen3-32B` snapshot (e.g. via `huggingface-cli download` or a symlink to a shared cache) before running.

(`Qwen/Qwen3-32B` BF16, ~64 GiB across 17 safetensors shards.)

Disable thinking mode in the chat template (`enable_thinking=False`) — code edits at `max_tokens=512` would otherwise get eaten by Qwen3's `<think>...</think>` preamble.
