# Qwen3.5-397B-A17B From-Scratch 4xB200 Input

Use:

- `--input examples/model-serving/qwen3.5-397b-a17b-4xb200`
- `--runs-dir /work/vibesys-runs`
- `--local`
- `--interface service`
- `--modal` for B200-backed runs

The objective, checkers, and benchmark live in `.vibesys/tasks/default/`; the
single task is selected automatically, so no `--task` flag is needed.

This input provides no serving engine and no pre-cloned engine source: the
candidate builds the large hybrid Gated-DeltaNet/MoE serving stack for
`Qwen/Qwen3.5-397B-A17B` from scratch and distributes it across 4 B200s. It
is the from-scratch counterpart to the `-vllm` and `-sglang` bundles.
