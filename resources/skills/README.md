# Skills

This directory contains VibeSys's bundled Agent Skills and reference material.
It is the default skill candidate root (`--skills-dir`); skills discovered here
are copied into each run's workspace for the coding agents to read.

## Collections

- [`serving-systems/`](serving-systems/) — LLM and multimodal serving-system
  development: model architectures, serving algorithms, kernel-library
  backends, hardware notes, and source maps into vLLM / SGLang / TensorRT-LLM.
  See its [SKILL.md](serving-systems/SKILL.md) router and
  [CLAUDE.md](serving-systems/CLAUDE.md) authoring guide.
- [`neuron-agentic-development/`](neuron-agentic-development/) — vendored,
  unmodified NKI (Neuron Kernel Interface) skills from AWS's
  `aws-neuron/neuron-agentic-development`, loaded only for
  `--backend trainium` via its `.vibesys.toml` sidecar.

For VibeSys-specific, optional sidecar metadata such as backend-scoped skill
rules, see [Skill Metadata](../../docs/skill-metadata.md).
