# Vendored: AWS Neuron Agentic Development (NKI skills)

These are **vendored, unmodified** Agent Skills from AWS's open-source
[`aws-neuron/neuron-agentic-development`](https://github.com/aws-neuron/neuron-agentic-development),
licensed under **Apache-2.0** (see `LICENSE.txt` / `NOTICE`).

They teach an agent to author, debug, and profile **NKI** (Neuron Kernel
Interface) kernels for AWS Trainium / Inferentia. VibeServe surfaces them
automatically when running with `--backend trainium` (see `.vibeserve.toml` and
`docs/skill-metadata.md`), so the implementer can write custom NeuronCore
kernels instead of treating the device as a black box.

## What's vendored

Only the NKI skills (`skills/neuron-nki-*`); the `neuron-framework-*`,
`agents/`, `hooks/`, and Python packaging from upstream are intentionally
omitted.

| Skill | Purpose |
| ----- | ------- |
| `neuron-nki-writing` | Write/modify NKI kernels (PyTorch/NumPy/NL → NKI). |
| `neuron-nki-docs` | NKI API signatures, tutorials, hardware-arch reference. |
| `neuron-nki-debugging` | Resolve NKI compile/execution errors. |
| `neuron-nki-profiling` | Capture on-hardware NKI profiles via neuron-explorer. |
| `neuron-nki-profile-querying` | Query NEFF/NTFF profiles to localize bottlenecks. |

## Provenance

- Upstream: https://github.com/aws-neuron/neuron-agentic-development
- Pinned commit: `648923a2065f61b771dd8a2386236dae846078cc`
- License: Apache-2.0

**Do not hand-edit** these files — they are a vendored copy. Re-vendor with
`./update.sh` to pull a newer upstream revision.
