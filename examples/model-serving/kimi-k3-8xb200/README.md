# Kimi-K3 From-Scratch 8xB200 Input

Use:

- `--input examples/model-serving/kimi-k3-8xb200`
- `--runs-dir /work/vibesys-runs`
- `--local`
- `--interface service`
- `--modal` for B200-backed runs

The objective, checkers, and benchmark live in `.vibesys/tasks/default/`; the
single task is selected automatically, so no `--task` flag is needed.

This input provides no serving engine and no pre-cloned engine source: the candidate
builds the frontier-scale MoE serving stack for `moonshotai/Kimi-K3` (2.8T
total / ~104B active MXFP4 MoE, 896 experts) from scratch and distributes it
across 8xB200. It is the from-scratch counterpart to `kimi-k3-8xb200-sglang`.
This is a frontier-scale, memory-bandwidth-bound serving problem; single-node
8xB200 sits at the edge of the model's footprint and a multi-node deployment
may be required for long-context headroom.
