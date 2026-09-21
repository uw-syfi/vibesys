Reference bundle. Read for correctness; do **not** import the modeling file at
runtime.

- `modeling_qwen3_5_moe.py`, `configuration_qwen3_5_moe.py`: verbatim copies of
  `transformers/models/qwen3_5_moe/` from transformers **5.12.1** (the MoE file
  imports nothing from `qwen3_5`, only shared transformers utilities). The vision
  tower and multimodal wrapper in the file are irrelevant for text serving; the
  text path is `Qwen3_5MoeTextModel` and `Qwen3_5MoeForCausalLM`.
- `config.json`: the real `config.json` of `amd/Qwen3.5-397B-A17B-MXFP4`
  (fetched from huggingface.co, unmodified, including `quantization_config`).
- `meta.json`: pins the HuggingFace model id. Materialize weights with
  `huggingface-cli download amd/Qwen3.5-397B-A17B-MXFP4 --local-dir model`
  (94 safetensors shards, no `model.safetensors.index.json`; scan shard headers).

## Architecture (text path)

60 layers, hidden 4096, pattern `[linear, linear, linear, full] x 15`
(`full_attention_interval` 4). Every layer has a sparse MoE MLP (512 experts,
top 10, expert width 1024, plus one shared expert of width 1024 with a sigmoid
gate). Full attention: 32 q heads, 2 kv heads, head_dim 256, q_proj is 2x wide
(query and output gate), q/k RMSNorm per head, partial rotary (0.25 -> 64 dims,
theta 1e7; text-only mRoPE reduces to plain RoPE). Gated DeltaNet: 16 k heads x
128, 64 v heads x 128, conv kernel 4 over the concatenated q|k|v (12288
channels), fp32 recurrent state per layer of shape `[64, 128, 128]`. RMSNorm is
`x * (1 + w)`, except the DeltaNet gated norm which uses plain `w`.

## Checkpoint tensor layout (verified from all 94 shard headers)

Names use the `model.language_model.` prefix; `lm_head.weight` is untied.

| tensor | dtype / shape |
|---|---|
| `embed_tokens.weight`, `lm_head.weight` | BF16 `[248320, 4096]` |
| `layers.N.input_layernorm.weight`, `post_attention_layernorm.weight`, `norm.weight` | BF16 `[4096]` |
| `layers.N.linear_attn.in_proj_qkv.weight` | BF16 `[12288, 4096]` |
| `.in_proj_z.weight` / `.in_proj_a` / `.in_proj_b` | BF16 `[8192,4096]` / `[64,4096]` / `[64,4096]` |
| `.conv1d.weight` | BF16 `[12288, 1, 4]` (no bias) |
| `.A_log`, `.dt_bias` | BF16 `[64]` |
| `.norm.weight`, `.out_proj.weight` | BF16 `[128]`, `[4096, 8192]` |
| `layers.N.self_attn.{q,k,v,o}_proj.weight` | BF16 `[16384,4096]`, `[512,4096]` x2, `[4096,8192]` |
| `.q_norm.weight`, `.k_norm.weight` | BF16 `[256]` |
| `layers.N.mlp.gate.weight` (router) | BF16 `[512, 4096]` |
| `layers.N.mlp.shared_expert.{gate,up,down}_proj.weight`, `shared_expert_gate.weight` | BF16 |
| `layers.N.mlp.experts.E.gate_proj.weight`, `.up_proj.weight` | **U8** `[1024, 2048]` |
| `layers.N.mlp.experts.E.down_proj.weight` | **U8** `[4096, 512]` |
| `...experts.E.{gate,up}_proj.weight_scale` | **U8** `[1024, 128]` |
| `...experts.E.down_proj.weight_scale` | **U8** `[4096, 32]` |

Experts are stored per expert and unfused (HF fuses them into 3D `gate_up_proj`
and `down_proj` parameters; `gate_up` is `[gate; up]` on dim 0). There is no
`input_scale`: activation quantization is dynamic and is not stored.
Not needed for text serving: `model.visual.*` (BF16) and `mtp.*` (one MTP block
with unquantized BF16 experts, `mtp_num_hidden_layers` 1).

Only the routed experts are quantized (model card: "Experts in language model
only"). Everything else, including attention, DeltaNet projections, router,
shared expert, embeddings and lm_head, is BF16. `quantization_config.exclude`
lists 2287 non-quantized module names.

## MXFP4 packing

OCP MX format: weights are fp4 e2m1 (magnitudes `0, .5, 1, 1.5, 2, 3, 4, 6`,
bit 3 is the sign), two per byte, blocks of 32 elements along the input (K)
dimension share one e8m0 scale (`2^(byte - 127)`). So a `[N, K]` weight is
stored as U8 `[N, K/2]` plus U8 scales `[N, K/32]`.

Verified against the FP8 source checkpoint (`Qwen/Qwen3.5-397B-A17B-FP8`):
dequantizing `layers.43.mlp.experts.0.gate_proj` with **low nibble = even
element, high nibble = odd element** and the scale above matches the FP8-dequant
weight with correlation 0.994 (relative error 11%, i.e. fp4 quantization error).
The other nibble orders tried (high first, halves, 16-wide block halves) give
correlation ~0. `quantization_config.export.pack_method` says `"reorder"`, but
the measured layout is plain low-nibble-first for this tensor. Not checked: e8m0
value 255 (NaN in the OCP spec; ignored), other layers or `down_proj`.

## Memory

Routed experts: 60 x 512 x 3 x (1024 x 4096) = 386.5 B params. At 4.25 bits
(fp4 + 1/32 byte scale) that is about 205 GB. BF16 would be 773 GB, more than
4 x 128 GB = 512 GB, so experts must stay MXFP4 in memory. Non-expert weights
are about 10 B params (about 20 GB BF16), including 2 x 2 GB for embedding and
lm_head. Total resident weights are about 225 GB, roughly 56 GB per device with
a contiguous 15-layer split.

## Not verified

- Only the tensors above were checked for nibble order (one expert tensor);
  no end-to-end logits against real weights have been compared.
- `reference/config.json` was fetched live; `transformers_version` in it is
  `4.57.0.dev0` while the copied modeling code is from 5.12.1.
