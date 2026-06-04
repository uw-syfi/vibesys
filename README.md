# VibeServe: Can AI Agents Build Bespoke LLM Serving Systems?

[![arXiv](https://img.shields.io/badge/arXiv-2605.06068-b31b1b.svg)](https://arxiv.org/abs/2605.06068)

**An agentic loop that synthesizes bespoke LLM serving systems — one per (model, hardware, workload) target — instead of forcing every deployment through a single general-purpose runtime.**

<p align="center">
  <img src="docs/figures/idea.png" width="85%" alt="Generic serving today vs. VibeServe's per-target bespoke systems">
</p>

## Updates

- **2026-05** — Blog post: [Let AI Agents Write Your Serving Stack with VibeServe](https://syfi.cs.washington.edu/blog/2026-05-12-introducing-vibeserve/).
- **2026-05** — Paper released on arXiv: [2605.06068](https://arxiv.org/abs/2605.06068).

## Introduction

VibeServe explores a new approach to LLM serving: instead of relying on one general-purpose runtime to support every model, workload, and hardware target, we use AI agents to generate bespoke serving systems for each deployment scenario. The project asks whether long-horizon coding agents can synthesize complete LLM serving stacks end-to-end, including scheduling, caching, runtime logic, correctness checks, and performance optimizations tailored to a specific target.

The system is organized as a multi-agent optimization loop. An outer loop plans the search over system designs using persistent state such as issues, memory, and git history, while an inner loop implements candidate systems, validates correctness against a reference implementation, and measures performance on the target benchmark. Across standard and non-standard serving scenarios, VibeServe matches highly optimized systems like vLLM in mainstream deployments and achieves substantial gains in specialized settings involving predicted-output decoding, hybrid prompt caching, streaming ASR, constrained JSON decoding, multimodal inference, and Apple Silicon deployment.

## Architecture

<p align="center">
  <img src="docs/figures/architecture.png" width="90%" alt="VibeServe architecture: outer loop dispatches per-round tasks to an inner loop of Implementer / Accuracy Judge / Performance Evaluator agents">
</p>

The framework factors the work along two axes:

- **Outer loop** — a search policy operating over a git-recorded history of validated checkpoints. It picks the next optimization, dispatches one concrete task to the inner loop, and updates persistent planning state (issues, long-term memory file, commit graph). 
- **Inner loop** — three role-specialized coding-agent invocations on a shared workspace:
  - *Implementer* writes/edits the candidate serving system.
  - *Accuracy Judge* runs the user-supplied checker against the reference and inspects diffs/runtime behavior for reward-hacking patterns; only correct candidates exit the inner loop.
  - *Performance Evaluator* profiles the implementation (Nsight Systems, PyTorch profiler) and feeds bottleneck hints back to the policy.
- **Skills library** — Agent Skills entries distilled from existing serving engines and research literature (continuous batching, paged-KV, FlashInfer/FlashAttention, MLX, hybrid-cache management, …). New model families, hardware platforms, and optimization techniques are added by writing a skill, not by modifying the framework.
- **Execution environment** — an isolated workspace that mounts the user-provided artifacts read-only (so the Implementer cannot edit the checker or reference) and exposes the target hardware (local CUDA, Modal, Docker, or Apple Silicon) plus profilers.

Each candidate is a git commit; the outer loop only advances on Judge-validated implementations, so incorrect candidates can never derail subsequent rounds.

## Installation

Requires Python 3.11+.

```bash
uv sync
cp .env.example .env       # provider keys (Anthropic / OpenAI / Vertex / …)
cp agent.toml.example agent.toml
```

## Quickstart

```bash
# Issue-tracker outer loop, Codex CLI, Docker on local CUDA, 4 rounds
vibe-serve \
  --ref examples/moonshine-streaming/reference \
  --acc-checker examples/moonshine-streaming/accuracy_checker \
  --bench examples/moonshine-streaming/benchmark \
  --exp-name my-experiment \
  --docker \
  --agent-backend cli --cli-provider codex \
  --max-rounds 4 \
  --modality speech_to_text
```

`--outer-loop` defaults to `agent`.  Pass `--outer-loop plain` or `--outer-loop evolve` to switch.  See `vibe-serve --outer-loop <kind> --help` for loop-specific flags.

See `vibe-serve --outer-loop <kind> --help` for loop-specific flags.

A separate entry point exposes the issue MCP server used by the plain loop:

```bash
vibe-serve-issue-mcp                         # serves issues.json over MCP
```

## Per-target inputs

Each evaluation target lives under `examples/<name>/`:

```
examples/<name>/
├── OBJECTIVE.md          # free-form deployment goal (model + hardware + workload + interface)
├── reference/            # reference HuggingFace Transformers implementation
│   ├── reference.py
│   ├── config.json
│   └── meta.json         # model id + revision
├── accuracy_checker/     # checker.py + tests/data — the correctness gate
├── benchmark/            # benchmark.py + load levels — emits the metric to optimize
└── README.md             # human-readable description
```

`OBJECTIVE.md` is read at the start of every run and must live next to `--ref` (sibling, not inside). See `examples/Llama-3-8B/`, `examples/moonshine-streaming/`, `examples/qwen3-32b-code-edit/`, `examples/olmo-hybrid-prefix-caching/`, `examples/Llama-3.1-8B-Instruct-MLX-8bit/`, `examples/show-o2-1.5B-HQ-h100/`, and `examples/show-o2-1.5B-HQ-macbook/` for the paper scenarios.

For multi-objective evolutionary runs, drop an `objectives.toml` next to `OBJECTIVE.md` (or pass `--objective name:max|min` flags) — see `vibe-serve --outer-loop evolve --help`.

## Configuration (`agent.toml`)

```toml
[model]
name = "claude-sonnet-4-6"   # auto-detected provider for claude-* / gpt-* / gemini-*
# provider = "anthropic"     # optional override

[backend]
name = "cuda"                 # or "metal" for Apple Silicon (local exec only)

[agent]
backend = "cli"               # "cli" (codex/claude/gemini/opencode) or "deepagents"
cli_provider = "codex"        # which coding-agent harness to drive
# cli_model = "gpt-5-codex"   # override the model the CLI tool uses
# cli_timeout = 1800          # per-invocation timeout (seconds)

# Optional: benchmark load levels handed to the perf evaluator.
# [[perf_eval.load_levels]]
# rate = 1
# duration = 20
# max_tokens = 128
```

Provider credentials live in `.env` — see `.env.example`. The CLI flags `--agent-backend` / `--cli-provider` / `--backend` override these.

The config is validated against a typed schema on load (`vibe_serve/config.py`): unknown sections or keys, unknown providers/backends, and missing required fields are rejected with an error rather than silently ignored.

## Skills library

`resources/skills/serving-systems/` contains the Agent Skills entries the inner loop's agents read at runtime: model architectures, serving algorithms, programming frameworks, backend libraries, hardware platforms, and reference engines. New optimization techniques and model families enter as new skill entries; the framework itself is target-agnostic.

## Outputs

Every run creates `exp_env/<timestamp>-<name>/`:

```
exp_env/<run>/
├── workspace/                # the unified, git-tracked workspace (each round = one commit)
├── logs/
│   ├── run-*.log             # top-level run log
│   ├── run-*-roundNNN.log    # per-round agent log (agent loop)
│   ├── progress.md           # long-term memory file the Orchestrator reads/edits
│   ├── rounds.json           # per-round audit
│   ├── state.json            # cursor (plain loop)
│   ├── issues.json           # IssueBoard (plain loop)
│   ├── population.json       # Individual list (evolve loop)
│   └── docker.log
└── reference/                # snapshot of --ref at start
```

Resume any run with `--resume` (defaults to "latest"):

```bash
vibe-serve --resume                  # newest run
vibe-serve --resume 20260507-...     # specific dir
```

## Repository layout

```
src/vibe_serve/
├── cli.py                        # single entry point: `vibe-serve`
├── context.py                    # _RunContext: lifecycle + ctx.invoke()
├── agent_runner.py               # invoke wrappers + structured-response extraction
├── prompts.py                    # Jinja + backend-fragment renderer
├── schemas.py                    # Pydantic response schemas
├── llm_client.py                 # LLM client factory
├── config.py / constants.py
│
├── loops/                        # the three outer-loop search policies
│   ├── agent/                    # issue-tracker (Orchestrator-driven)
│   ├── plain/                    # Ralph-style queue-drain
│   ├── evolve/                   # population-based
│   └── profiler.py               # shared Performance Evaluator helper
│
├── sandbox/                      # execution-environment policy
│   ├── docker_sandbox.py
│   ├── modal_sandbox.py
│   ├── modal_model_setup.py
│   └── run_environment.py
│
├── agents/                       # coding-agent harness abstraction
│   └── callbacks.py              # LangChain logger (deepagents path)
└── backends/                     # cuda / metal compute backends

examples/                         # six paper scenarios + nsys/torch profiler skills
resources/skills/serving-systems/ # Agent Skills library
```

- **agent**: pre-round → profiler → orchestrator plan → implementer/judge
  retry up to `--max-retries-per-round` (default 3).  Always exhausts
  `--max-rounds`; supports `revert_to_round` mid-loop.
- **plain**: drain `IssueBoard` (one impl + one judge per issue, BLOCK
  after `--max-attempts-per-issue`) → `perf_eval` (may file new issues).
  Early-exits when queue is empty and `perf_eval` files nothing.
- **evolve**: per generation × child: select parent (Pareto frontier with
  `--frontier-bias`, scalar softmax otherwise) + inspirations →
  `git checkout` parent tree → mutator → judge → profiler → commit.
  No early stop; runs the full `--max-generations × --children-per-generation`.

## Development

```bash
uv run pytest                                       # full suite
uv run pytest tests/loops/plain/test_plain_loop.py  # one file
uv run pytest -k orchestrator                       # by keyword
```

## Citation

If you use VibeServe in your research, please cite:

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
