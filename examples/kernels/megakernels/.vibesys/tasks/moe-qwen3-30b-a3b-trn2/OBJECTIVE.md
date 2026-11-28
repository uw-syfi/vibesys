# Qwen3-30B-A3B decode step as one megakernel on AWS Trainium2

Write `trn2/moe-qwen3-30b-a3b/submission.py`, and any helper files of your own under `trn2/moe-qwen3-30b-a3b/`, so that
`build(case)` returns `run(inputs)`, which computes the whole Qwen/Qwen3-30B-A3B decode step that
`.vibesys/shared/megabench/tasks/moe.py` defines, in **one NKI kernel call per step and
rank**: one kernel that runs the whole step as phases. `build` receives the geometry in
`.vibesys/tasks/moe-qwen3-30b-a3b-trn2/case.json` plus `case['execution']`, and may compile and allocate once;
`run` returns `logits`, `next_token`, `k_write`, `v_write`. The score is the step's kernel latency (Neuron device events, p50,
slowest rank; `kernel_latency_ms`), lower is better.

The model is [Qwen/Qwen3-30B-A3B](https://huggingface.co/Qwen/Qwen3-30B-A3B) at batch 1. The reference module and the
helpers it imports are the exact PyTorch math, the input layouts and the tolerances.

The evaluator starts 4 rank processes, one per logical NeuronCore (device
`neuron:<rank>`). `case['execution']` holds rank, world_size, tp_rank, device, backend
and the live neuron tp_group. Each rank receives its shard of the inputs and returns its
shard of the outputs, as `.vibesys/shared/megabench/tasks/sharding.py` defines; the
harness reassembles them and grades the single-device reference's outputs. Do cross-core
communication inside the kernel with `nki.collectives`.

## Rules

- Every output must match the PyTorch reference within the case's tolerances, on fresh random
  inputs and weights each time; integer outputs such as tokens must match exactly.
- Compute every output from the current inputs and weights on every call. Never modify an
  input.
- At most one NKI kernel call per step and rank (a `torch_neuronx.nki_op` or `nki.jit`
  kernel), and no other device work: no eager PyTorch ops, `torch.compile` graphs or
  `torch.distributed` collectives on Neuron tensors. The check audits this.
- Edit only `trn2/moe-qwen3-30b-a3b/`. Never edit `.vibesys/`.

## Machine

An AWS trn2.3xlarge: one Trainium2 chip (LNC=2: four logical NeuronCores of 24 GiB HBM each),
of which this task uses all four. The default Python has PyTorch with torch-neuronx (device
`neuron`), NKI (`nki`) and `neuronx-cc`; the Neuron tools (`neuron-ls`, `neuron-explorer`)
are in `/opt/aws/neuron/bin`.

## Check and benchmark

Both commands run `.vibesys/shared/evaluate.py` on `trn2/moe-qwen3-30b-a3b/submission.py`. The harness builds
each trial's inputs on the CPU and runs the step on the NeuronCores. The check compares three
trials against the reference and audits the kernel calls of one step. The benchmark repeats the check
and times the step; a candidate that fails any part has no `kernel_latency_ms` reading.
