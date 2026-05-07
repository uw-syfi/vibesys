Reference bundle.

- `reference.py` — verbatim copy of
  `transformers/models/olmo_hybrid/modeling_olmo_hybrid.py` (HF transformers
  main). Read for correctness; do **not** import it at runtime.
- `config.json` — `allenai/Olmo-Hybrid-7B` config (32 layers, 3 linear :
  1 full attention pattern, vocab 100 352).
- `meta.json` — pins the HuggingFace model id (`allenai/Olmo-Hybrid-7B`).
  Materialize weights with e.g.
  `huggingface-cli download allenai/Olmo-Hybrid-7B --local-dir model`.
