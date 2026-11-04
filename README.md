# VibeSys: Generating Bespoke Systems with AI Agents

[![arXiv](https://img.shields.io/badge/arXiv-2605.06068-b31b1b.svg)](https://arxiv.org/abs/2605.06068)

**An agentic framework that generates bespoke systems from application requirements, workload characteristics, and the underlying hardware.**

## News

- **2026-11-04** — [VibeSys on Trainium](https://syfi.cs.washington.edu/blog/2026-11-04-vibesys-trainium/): agents build and tune Qwen3.8 serving engines on AWS Trainium2.
- **2026-10-08** — [VibeSys on AMD MI300A](https://syfi.cs.washington.edu/blog/2026-10-08-qwen35-mi300a/): an agent-built Qwen3.5-397B engine achieves 2.3× the goodput of tuned SGLang.
- **2026-05-12** — [Introducing VibeServe](https://syfi.cs.washington.edu/blog/2026-05-12-introducing-vibeserve/): let AI agents write your serving stack. [Paper](https://arxiv.org/abs/2605.06068).

## About VibeSys

VibeSys explores a broader approach to systems development: use application
requirements, workload characteristics, and hardware capabilities as the inputs
to an agentic search process that creates a purpose-built system. Each target
defines its own implementation contract, correctness checks, and performance
benchmark, allowing VibeSys to work across domains rather than assuming a single
runtime, programming language, or deployment shape.

One of VibeSys's first initiatives is [**VibeServe**](https://arxiv.org/abs/2605.06068), which asks whether AI agents
can generate a bespoke LLM serving system for each model, workload, and hardware
target. The figures, blog post, and paper below document that initiative.

<p align="center">
  <img src="docs/figures/idea.png" width="85%" alt="Generic serving today vs. VibeServe's per-target bespoke systems">
</p>

## Quickstart

VibeSys is available as a plugin for Claude Code and Codex. In Claude Code, for example:

```bash
claude plugin marketplace add uw-syfi/vibesys
claude plugin install vibesys@vibesys
# Install additional plugins for domain-specific knowledge
claude plugin install vibesys-llm-serving@vibesys
```

Then ask your agent to create a task describing your optimization goal and correctnes criteria, and start the optimization:

```text
/vibesys:create-task
/vibesys:optimize <task>
```

See the [skills installation guide](skills/README.md#install) for Codex,
`npx skills`, and domain plugins for databases, microservices, and kernels.

## Performance highlights

Below are some recent optimization results from running VibeSys. Click on the images to see blog posts detailing the results.

<table>
  <tr>
    <td colspan="2" align="center">
      <a href="https://syfi.cs.washington.edu/blog/2026-10-08-qwen35-mi300a/">
        <img src="docs/figures/qwen35-397b-mi300a.png" width="900" alt="Qwen3.5-397B on four AMD MI300As: goodput improves across optimization rounds and exceeds tuned SGLang">
      </a>
      <br><strong>Qwen3.5-397B · 4× AMD MI300A</strong><br>
      2,242 tok/s goodput · <strong>2.3× tuned SGLang</strong>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="https://syfi.cs.washington.edu/blog/2026-11-04-vibesys-trainium/">
        <img src="docs/figures/qwen38-27b-trainium2.png" width="450" alt="Qwen3.8-27B on one Trainium2 chip: throughput rises from 195 to 861 tokens per second across optimization rounds">
      </a>
      <br><strong>Qwen3.8-27B · 1× Trainium2</strong><br>
      195 → 861 tok/s · <strong>4.4× the first working engine</strong>
    </td>
    <td width="50%" align="center">
      <a href="https://syfi.cs.washington.edu/blog/2026-11-04-vibesys-trainium/">
        <img src="docs/figures/qwen38-flash-next-trainium2.png" width="450" alt="Qwen3.8-Flash-Next on sixteen Trainium2 chips: throughput improves across twelve optimization rounds">
      </a>
      <br><strong>Qwen3.8-Flash-Next · 16× Trainium2</strong><br>
      <strong>3.1× the starting engine</strong>
    </td>
  </tr>
</table>

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
