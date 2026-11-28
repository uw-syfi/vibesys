# Megakernels

Write one GPU kernel that runs a whole model step, then make it fast. Each task gives the
agent a PyTorch reference for one step of a real model architecture at batch 1 and asks for
a submission that computes the same outputs in **a single kernel launch**. The score is the
step's kernel latency.

These are the workloads of the VibeSys megakernel blog post. Run them with the divide-and-
conquer mode of `/vibesys:optimize`, described in
[Writing Megakernels](../../../docs/megakernel-divide-and-conquer.md):

```bash
vibesys example new kernels/megakernels ~/work/megakernels
cd ~/work/megakernels && claude
```

and, in Claude Code with the `vibesys` plugin and the kernels plugin for the hardware
(`vibesys-kernels-cuda`, `vibesys-kernels-rocm` or `vibesys-kernels-trainium`) installed:

```text
/vibesys:optimize dense-qwen3-0.6b-b300 --megakernel-divide-and-conquer --budget time 285m --fanout 3
```

## Tasks

| Workload | Model | Step | NVIDIA B300 task | AMD MI350X task | AWS Trainium2 task |
| --- | --- | --- | --- | --- | --- |
| Dense LLM | [Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B) | decode, 128-token KV cache | `dense-qwen3-0.6b-b300` | `dense-qwen3-0.6b-mi350x` | `dense-qwen3-0.6b-trn2` |
| MoE LLM | [Qwen3-30B-A3B](https://huggingface.co/Qwen/Qwen3-30B-A3B) | decode, 128-token KV cache | `moe-qwen3-30b-a3b-b300` | `moe-qwen3-30b-a3b-mi350x` | `moe-qwen3-30b-a3b-trn2` |
| W8 LLM | [Gemma 3 4B](https://huggingface.co/google/gemma-3-4b-it), 8-bit weights | decode, 128-token KV cache | `w8-gemma3-4b-b300` | `w8-gemma3-4b-mi350x` | `w8-gemma3-4b-trn2` |
| W4 LLM | [Gemma 3 4B](https://huggingface.co/google/gemma-3-4b-it), 4-bit weights | decode, 128-token KV cache | `w4-gemma3-4b-b300` | `w4-gemma3-4b-mi350x` | `w4-gemma3-4b-trn2` |
| Text-to-speech | [CSM-1B](https://huggingface.co/sesame/csm-1b) | one audio frame: a backbone decode step, then the depth decoder over 32 codebooks | `tts-csm-1b-b300` | `tts-csm-1b-mi350x` | `tts-csm-1b-trn2` |
| Vision-language-action | [pi0.5](https://huggingface.co/lerobot/pi05_base) | one action chunk: the action expert's flow-matching steps over a cached prefix | `vla-pi05-b300` | `vla-pi05-mi350x` | `vla-pi05-trn2` |
| World model | [Waypoint-1.5-1B](https://huggingface.co/Overworld/Waypoint-1.5-1B) | one latent frame: four denoising passes and a cache pass | `world-waypoint1.5-1b-b300` | `world-waypoint1.5-1b-mi350x` | `world-waypoint1.5-1b-trn2` |

Each task's `OBJECTIVE.md` states the interface, the outputs and the rules. The candidate for
task `<workload>-<hw>` is `<hw>/<workload>/submission.py`, which starts as a stub. It exports
`build(case) -> run(inputs) -> outputs`; `build` may compile and allocate once, and every
`run` must be one kernel launch (on Trainium2, one NKI kernel call per NeuronCore rank).

On Trainium2 the four LLM tasks use the whole chip as four tensor-parallel ranks, one per
logical NeuronCore: each rank gets its shard of the inputs, as
`.vibesys/shared/megabench/tasks/sharding.py` defines, and the harness grades the reassembled
outputs against the single-device reference. The three multimodal tasks run on one NeuronCore
with whole inputs.

## Evaluation

Both the check and the benchmark run [`evaluate.py`](.vibesys/shared/evaluate.py), which
loads the trusted harness in [`.vibesys/shared/megabench/`](.vibesys/shared/megabench/). For
each call it builds fresh random inputs and weights, runs the PyTorch reference from
`megabench/tasks/<module>.py`, and compares the candidate's outputs within the case's
tolerances (tokens and codes exactly). It then counts the GPU launches of one step with the
profiler and fails a step over its budget of one. The benchmark times the step with device
events and reports the p50 as `kernel_latency_ms`; a candidate that fails any part has no
reading, so it can never win on speed.

## Requirements

A machine with the task's GPU, its compiler toolchain, and PyTorch with a matching GPU build
in the `python` on `PATH`. The B300 runs used CUDA 13.1 and PyTorch 2.13; the MI350X runs used
ROCm 7.1 and PyTorch 2.10. As in those runs, the MI350X benchmark times the candidate in one
more fresh process and scores the fast reading when the two disagree. The Trainium2 runs used a
trn2.3xlarge with PyTorch 2.14, torch-neuronx 2.14.3, NKI and `neuronx-cc` from the Neuron
SDK; there the harness builds each trial's inputs on the CPU, so Waypoint's check and benchmark
have longer limits and its benchmark runs one trial. Nothing is downloaded: the weights are
random, with each model's real geometry.
