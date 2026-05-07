# vibe-serve

LLM-agent loop that builds ML inference servers from a reference HuggingFace
implementation.  An implementer agent writes the server, a judge reviews it
against pass criteria + accuracy + benchmark, and a profiler ranks
bottlenecks for the next round.

Three outer loops are available, all behind a single CLI:

| `--outer-loop` | What drives the loop | Planning artifact |
|---|---|---|
| `agent` (default) | An LLM **Orchestrator** picks each round's task | `roadmap.md` + `progress.md` (the *issue board*) |
| `plain` | Deterministic queue drain | `IssueBoard` (`issues.json`) — `perf_eval` files issues, implementer drains them |
| `evolve` | Population-based mutation/selection | n/a (each offspring is a git commit) |

## Installation

Requires Python 3.11+.

```bash
uv sync
cp .env.example .env       # fill in provider keys
cp agent.toml.example agent.toml
```

## Quickstart

```bash
# Agent loop, codex CLI, Docker on local CUDA, 4 rounds
vibe-serve \
  --ref inputs/moonshine-streaming/reference \
  --acc-checker inputs/moonshine-streaming/accuracy_checker \
  --bench inputs/moonshine-streaming/benchmark \
  --exp-name my-experiment \
  --docker \
  --agent-backend cli --cli-provider codex \
  --max-rounds 4 \
  --modality speech_to_text
```

`--outer-loop` defaults to `agent`.  Pass `--outer-loop plain` or
`--outer-loop evolve` to switch.  See `vibe-serve --outer-loop <kind> --help`
for loop-specific flags.

The **issue MCP server** (used by the plain loop's judge to file new
issues during a round) is exposed as a separate entry point:

```bash
vibe-serve-issue-mcp                         # serves issues.json over MCP
```

## Inputs

Each model lives under `inputs/<name>/`:

```
inputs/<name>/
├── OBJECTIVE.md          # free-form goal handed to the orchestrator
├── reference/            # reference HuggingFace implementation
│   ├── reference.py
│   ├── config.json
│   └── meta.json         # model id + revision
├── accuracy_checker/     # checker.py + tests/data
├── benchmark/            # benchmark.py + load levels
└── README.md             # human-readable description
```

`OBJECTIVE.md` is read at the start of every run; it must live next to
`--ref` (sibling, not inside).  See `inputs/Llama-3-8B/`,
`inputs/moonshine-streaming/`, `inputs/qwen3-32b-code-edit/`, etc. for
working examples.

For multi-objective evolutionary runs, drop an `objectives.toml` next to
`OBJECTIVE.md` (or pass `--objective name:max|min` flags) — see
`vibe-serve --outer-loop evolve --help`.

## Configuration (`agent.toml`)

```toml
[model]
name = "claude-sonnet-4-6"   # auto-detected provider for claude-* / gpt-* / gemini-*
# provider = "anthropic"     # optional override

[backend]
name = "cuda"                 # or "metal" for Apple Silicon (local exec only)

[agent]
backend = "cli"               # "cli" (codex/claude/gemini/opencode) or "deepagents"
cli_provider = "codex"        # which CLI tool to drive
```

Provider credentials live in `.env` — see `.env.example`.  The CLI flags
`--agent-backend` / `--cli-provider` / `--backend` override this.

## Outputs

Every run creates `exp_env/<timestamp>-<name>/` containing:

```
exp_env/<run>/
├── workspace/                # the unified workspace (git-tracked)
├── logs/
│   ├── run-*.log             # top-level run log
│   ├── run-*-roundNNN.log    # per-round agent log (agent loop)
│   ├── progress.md           # per-round audit log (agent loop)
│   ├── rounds.json           # _RoundRecord audit (agent loop)
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

## Architecture

```
vibeserve_agent/
├── cli.py                        # single entry point: `vibe-serve`
├── context.py                    # _RunContext: lifecycle + ctx.invoke()
├── agent_runner.py               # invoke wrappers + structured-response extraction
├── prompts.py                    # Jinja + backend-fragment renderer
├── schemas.py                    # all Pydantic response schemas
├── llm_client.py                 # LLM client factory
├── config.py / constants.py
│
├── loops/                        # the three outer loops
│   ├── agent/                    # orchestrator-driven
│   ├── plain/                    # IssueBoard-driven
│   ├── evolve/                   # population-based
│   └── profiler.py               # shared profiler invoke helper
│
├── sandbox/                      # execution policy
│   ├── docker_sandbox.py
│   ├── modal_sandbox.py
│   ├── modal_model_setup.py
│   └── run_environment.py
│
├── agents/                       # agent-runner abstraction (deepagents vs cli)
│   └── callbacks.py              # LangChain logger (deepagents path)
└── backends/                     # cuda / metal compute backends
```

Per-loop algorithms (one round each):

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
uv run pytest                                  # full suite
uv run pytest tests/test_plain_loop.py         # one file
uv run pytest -k orchestrator                  # by keyword
```

The `agent.toml` lives at repo root; it is gitignored only for the
samples — commit your real configuration changes via PR.
