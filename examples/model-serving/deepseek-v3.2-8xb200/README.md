# DeepSeek-V3.2 From-Scratch 8xB200 Input

Use:

- `--input examples/model-serving/deepseek-v3.2-8xb200`
- `--runs-dir /work/vibesys-runs`
- `--local`
- `--interface service`
- `--modal` for B200-backed runs

The objective, checkers, and benchmark live in `.vibesys/tasks/default/`; the
single task is selected automatically, so no `--task` flag is needed.

This input provides no serving engine and no pre-cloned engine source: the candidate
builds the FP8 MoE serving stack for `deepseek-ai/DeepSeek-V3.2` (MLA +
DeepSeek Sparse Attention) from scratch and distributes it across 8xB200. It
is the from-scratch counterpart to `deepseek-v3.2-8xb200-sglang`. The
workload is 8k-token prompts with 1k-token outputs, and the optimization
target spans FP8 grouped-GEMM MoE kernels on Blackwell, large-scale expert
parallelism across 256 experts, and MLA/DSA long-context KV-cache management.
