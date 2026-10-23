# Vendored: AWS Neuron Agentic Development (NKI skills)

These are **vendored, unmodified** Agent Skills from AWS's open-source
[`aws-neuron/neuron-agentic-development`](https://github.com/aws-neuron/neuron-agentic-development),
licensed under **Apache-2.0** (see `LICENSE.txt` / `NOTICE`).

They teach an agent to author, debug, and profile **NKI** (Neuron Kernel
Interface) kernels for AWS Trainium / Inferentia. VibeSys surfaces them
automatically when running with `--backend trainium` (see `.vibesys.toml` and
`docs/contributing/skill-metadata.md`), so the implementer can write custom NeuronCore
kernels instead of treating the device as a black box.

## What's vendored

The NKI skills (`skills/neuron-nki-*`) and their profile-schema companion
(`skills/neuron-explorer-profile-schema`). Upstream's `neuron-framework-*`
skills (NxD Inference and vLLM-Neuron porting and profiling), the
`neuron-parallelism-planner` / `neuron-hardware-entitlement` planners,
`agents/`, `hooks/`, and Python packaging are intentionally omitted: VibeSys
tasks build their own serving stack on native TorchNeuron, and those skills
steer agents toward the stacks a task replaces.

| Skill | Purpose |
| ----- | ------- |
| `neuron-nki-writing` | Write/modify NKI kernels (PyTorch/NumPy/NL → NKI). |
| `neuron-nki-docs` | NKI API signatures, tutorials, hardware-arch reference. |
| `neuron-nki-debugging` | Resolve NKI compile/execution errors. |
| `neuron-nki-profiling` | Capture on-hardware NKI profiles via neuron-explorer. |
| `neuron-nki-profile-querying` | Query NEFF/NTFF profiles to localize bottlenecks. |
| `neuron-explorer-profile-schema` | What the neuron-explorer profile tables and columns mean. |

## Provenance

- Upstream: https://github.com/aws-neuron/neuron-agentic-development
- Pinned commit: `e63853627271bcb98d941c242641e698d210d5a0` (AWS Neuron 2.33,
  neuron-agentic-development 1.4)
- License: Apache-2.0

**Do not hand-edit** these files — they are a vendored copy. Re-vendor with
`./update.sh` to pull a newer upstream revision. The script applies one
fix-up: a note at the top of `neuron-nki-debugging/SKILL.md` saying its
`torch_xla` harness snippets predate native TorchNeuron.
