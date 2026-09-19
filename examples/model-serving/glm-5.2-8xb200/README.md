# GLM-5.2 From-Scratch 8xB200 Input

Use:

- `--input examples/model-serving/glm-5.2-8xb200`
- `--runs-dir /work/vibesys-runs`
- `--local`
- `--interface service`
- `--modal` for B200-backed runs

The objective, checkers, and benchmark live in `.vibesys/tasks/default/`; the
single task is selected automatically, so no `--task` flag is needed.

This input provides no serving engine and no pre-cloned engine source: the candidate
builds the FP8 sparse-MoE serving stack for `zai-org/GLM-5.2` (753B total
parameters, ~40B active per token) from scratch and distributes it across 8
B200s. It is the from-scratch counterpart to `glm-5.2-8xb200-sglang`.
