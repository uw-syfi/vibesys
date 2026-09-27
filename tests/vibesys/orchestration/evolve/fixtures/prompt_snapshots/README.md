# Evolve prompt snapshots

These fixtures contain the final prompts emitted by the evolve plugin after
domain interpolation and cold-start or offspring branching. The matrix covers
generic native and LLM-serving workloads for every evolve role.

Set `UPDATE_PROMPT_SNAPSHOTS=1` while running `test_prompts.py` to regenerate
them, then review every Markdown diff before committing it.
