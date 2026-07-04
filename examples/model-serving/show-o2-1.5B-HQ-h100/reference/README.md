Reference bundle for Show-o2 1.5B HQ.

Required files:
- `reference.py` — local loader and generation wrapper
- `config.json` — Show-o2 model config copied from the HF checkpoint
- `meta.json` — pinned model and Wan VAE metadata
- `Show-o/` — git submodule for the official Show-o repository, pinned to
  commit `45a5a2de01d1ebd10cd5864d29310a76476cdf23`; the wrapper imports from
  its `show-o2/` subdirectory

The wrapper exposes `ShowO2Model.from_pretrained(...)`, `generate_text(...)`,
and `generate_image(...)`. It lazily downloads weights if `reference/model`
does not exist.
