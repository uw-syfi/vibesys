Reference bundle for the MLX 8-bit Llama 3.1 8B Instruct target.

Backed by `mlx-community/Meta-Llama-3.1-8B-Instruct-8bit`, the model used as
the verifier/target in the speculative decoding playground.

Required files:
- `reference.py` — MLX-based reference inference and snapshot downloader
- `config.json` — model config copied from the pinned HF snapshot
- `meta.json` — pinned HF model id and revision
- `model/` — created lazily as a symlink to the downloaded HF snapshot

Run:

```bash
python reference.py --prompt "The capital of France is" --max-tokens 16
```
