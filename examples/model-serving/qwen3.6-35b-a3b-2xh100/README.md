# Qwen3.6-35B-A3B From-Scratch 2xH100 Input

Use:

- `--input examples/model-serving/qwen3.6-35b-a3b-2xh100`
- `--runs-dir /work/vibesys-runs`
- `--local`
- `--interface service`
- `--modal` for H100-backed runs

The objective, checkers, and benchmark live in `.vibesys/tasks/default/`; the
single task is selected automatically, so no `--task` flag is needed.

This input provides no serving engine and no pre-cloned engine source: the candidate
builds the sparse-MoE serving stack for `Qwen/Qwen3.6-35B-A3B` from scratch and
distributes it across 2 H100s. It is the from-scratch counterpart to
`qwen3.6-35b-a3b-2xh100-vllm`.
