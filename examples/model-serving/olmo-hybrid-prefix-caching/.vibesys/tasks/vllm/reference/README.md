Reference bundle.

- `reference.py` — verbatim copy of
  `transformers/models/olmo_hybrid/modeling_olmo_hybrid.py` (HF transformers
  main). Read for correctness; do **not** import it at runtime.
- `config.json` — `allenai/Olmo-Hybrid-7B` config (32 layers, 3 linear :
  1 full attention pattern, vocab 100 352).
- `meta.json` — pins the HuggingFace model id (`allenai/Olmo-Hybrid-7B`) and
  the revision the accuracy checker's reference outputs were generated from.
  Materialize weights with e.g.
  `huggingface-cli download allenai/Olmo-Hybrid-7B --revision 4f1cc566f9fdf3ce68da2ab6a788a83d89896dcf --local-dir model`.
