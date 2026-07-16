# LLM serving

**Use for:** building a bespoke LLM inference server (OpenAI-compatible or
similar) that the framework benchmarks for throughput/latency and checks for
output correctness.

**What this pack adds:**
- *Implementer:* points at the `serving-systems` skill / `references/` library
  (attention backends, CUDA graphs, speculative decoding, paged attention, …),
  notes that model weights live at `/model`, and warns that the Judge runs an
  accuracy + benchmark sanity check on top of the round criteria.
- *Judge:* the always-on correctness gates (`uv run pytest`, `/health` benchmark
  sanity, the accuracy checker's schema + sentinel rates), headline-metric
  performance judging, reward-hack / model-bypass detection, and scope /
  static-inspection discipline.

This reproduces vibesys's original serving-oriented prompts. Input bundles
select it with `[agent].domain = "llm-serving"`. The `single_agent` ablation
reuses a bespoke combined section below rather than the default implementer+judge
concatenation.
