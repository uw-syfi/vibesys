# VibeSys: Generating Bespoke Systems with AI Agents

[![arXiv](https://img.shields.io/badge/arXiv-2605.06068-b31b1b.svg)](https://arxiv.org/abs/2605.06068)

**An agentic framework that generates bespoke systems from application requirements, workload characteristics, and the underlying hardware.**

## News

- **2026-11-04** — [VibeSys on Trainium](https://syfi.cs.washington.edu/blog/2026-11-04-vibesys-trainium/): agents build and tune Qwen3.8 serving engines on AWS Trainium2.
- **2026-10-08** — [VibeSys on AMD MI300A](https://syfi.cs.washington.edu/blog/2026-10-08-qwen35-mi300a/): an agent-built Qwen3.5-397B engine achieves 2.3× the goodput of tuned SGLang.
- **2026-05-12** — [Introducing VibeServe](https://syfi.cs.washington.edu/blog/2026-05-12-introducing-vibeserve/): let AI agents write your serving stack. [Paper](https://arxiv.org/abs/2605.06068).

## Getting started

### Install skills for your coding agent

Start with our [Agent Skills](resources/skills/README.md) to give your coding
agent knowledge of serving systems, hardware, and performance optimization.
With [Node.js](https://nodejs.org/) installed, run this in your project directory:

```bash
npx skills add https://github.com/uw-syfi/vibesys/tree/main/resources/skills
```

Choose the skills and coding agent you want to use in the installer. For LLM
serving, start with [`serving-systems`](resources/skills/serving-systems/README.md).
Then ask your agent to optimize your system for your model, workload, and hardware,
with explicit correctness checks and a performance benchmark.

### Run the VibeSys optimization loop

To have VibeSys coordinate implementation, correctness checks, and benchmarking,
[install VibeSys and run your first task](#quickstart). Complete task definitions
are available in [`examples/`](examples/), and the
[running guide](docs/running-vibesys.md) covers setup and execution in detail.
VibeSys includes the bundled skills for its agents.

## About VibeSys

One of VibeSys's first initiatives is **VibeServe**, which asks whether AI agents
can generate a bespoke LLM serving system for each model, workload, and hardware
target. The figures, blog post, and paper below document that initiative.

<p align="center">
  <img src="docs/figures/idea.png" width="85%" alt="Generic serving today vs. VibeServe's per-target bespoke systems">
</p>

## Introduction

VibeSys explores a broader approach to systems development: use application
requirements, workload characteristics, and hardware capabilities as the inputs
to an agentic search process that creates a purpose-built system. Each target
defines its own implementation contract, correctness checks, and performance
benchmark, allowing VibeSys to work across domains rather than assuming a single
runtime, programming language, or deployment shape.

The framework is organized as a multi-agent optimization loop. An outer loop
plans the search over system designs using persistent state such as issues,
memory, and git history, while an inner loop implements candidates, validates
correctness against target-specific requirements, and measures performance on
the target workload and hardware. VibeServe is the first substantial initiative
built on this approach; its serving-focused results include predicted-output
decoding, hybrid prompt caching, streaming ASR, constrained JSON decoding,
multimodal inference, and Apple Silicon deployment.

## Architecture

<p align="center">
  <img src="docs/figures/architecture.png" width="90%" alt="VibeServe architecture: outer loop dispatches per-round tasks to an inner loop of Implementer / Accuracy Judge / Performance Evaluator agents">
</p>

The framework factors the work along two axes:

- **Outer loop** — a fresh designer selects one falsifiable causal hypothesis
  from git history, profiling evidence, and durable roadmap/progress memory, then
  hands it off until it is proven, disproven, or otherwise terminated.
- **Inner loop** — a hypothesis-scoped implementer session edits the candidate,
  chooses targeted experiments and parameter ranges, and reports whether to
  continue or nominate the result.
- **Independent judge** — a fresh, read-only reviewer checks the implementation,
  activation evidence, invariants, and reward-hacking risks at a sparse cadence.
  After a PASS, the framework—not an agent—runs and records the canonical
  accuracy and benchmark commands.
- **Performance evaluator** — profiles the implementation (Nsight Systems,
  PyTorch profiler) and feeds bottleneck hints into future design decisions.
- **Skills library** — Agent Skills entries distilled from existing serving engines and research literature (continuous batching, paged-KV, FlashInfer/FlashAttention, MLX, hybrid-cache management, …). New model families, hardware platforms, and optimization techniques are added by writing a skill, not by modifying the framework.
- **Execution environment**: an isolated runtime view where candidate source
  is writable while evaluator-owned inputs and framework metadata are read-only
  and integrity-checked. It exposes the target hardware (local CUDA, Modal,
  Docker, or Apple Silicon) plus profilers.

Each round is recorded in git and a framework-owned audit. Provisional rounds
remain explicitly unreviewed; only judge-approved candidates receive official
accuracy and performance results.

## Quickstart

Install Python 3.12+, Git, and [uv](https://docs.astral.sh/uv/). Agents run in
a local Docker container, so Docker Engine must be reachable without `sudo` (add
your user to the `docker` group). Tasks whose candidate is a container topology,
such as the `microservices` examples, declare `docker_in_docker = true`: their
container gets a Docker daemon of its own and needs the
[Sysbox](https://github.com/nestybox/sysbox) runtime installed. The `slurm` environment
with `[vibesys.agent_gpu]` also needs `bubblewrap` on Linux and on the compute nodes (see the
[CLI reference](docs/cli-flags.md)): it confines the GPU jobs its agent starts there.
Then install VibeSys:

```bash
uv tool install vibesys
```

Install and authenticate a supported coding-agent CLI. For Codex CLI, run
`codex login`; see the [CLI reference](docs/cli-flags.md) for other supported
agents.

From the root of the project you want to optimize, add a named task under
`.vibesys/tasks/`:

- `.vibesys/tasks/<task>/OBJECTIVE.md` describes what to optimize and the
  constraints the result must preserve.
- `.vibesys/tasks/<task>/vibesys.input.toml` identifies the problem domain and
  the programs that check correctness and benchmark performance.
- `agent.toml` optionally selects the coding agent, model, and hardware backend.
  Keep it untracked.

For example:

```toml
[model]
name = "gpt-5.4"

[agent]
backend = "cli"
cli_provider = "codex"

[backend]
name = "cpu"
```

See [`examples/`](examples/) for complete objectives and manifests across data
structures, model serving, and microservices. New examples are registered in
[`examples/registry.toml`](examples/registry.toml); see
[Adding an example](docs/contributing/examples.md).

Run from the project root:

```bash
cd /path/to/my-project
vibesys validate --task my-task
vibesys --task my-task --max-rounds 4
```

The directory must be its Git repository root, or outside Git so VibeSys can
initialize a repository. An existing repository needs a baseline commit and a
clean worktree. See [Running VibeSys](docs/running-vibesys.md) for copied
projects, examples, Docker, Modal, remote repositories, resume, and
alternate search loops. The [CLI reference](docs/cli-flags.md) documents every
flag. Contributor setup belongs in [`docs/contributing/development.md`](docs/contributing/development.md).

## Citation

If you use the VibeServe initiative in your research, please cite:

```bibtex
@misc{kamahori2026vibeserveaiagentsbuild,
      title={VibeServe: Can AI Agents Build Bespoke LLM Serving Systems?},
      author={Keisuke Kamahori and Shihang Li and Simon Peter and Baris Kasikci},
      year={2026},
      eprint={2605.06068},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2605.06068},
}
```
