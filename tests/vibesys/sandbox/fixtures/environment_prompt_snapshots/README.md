# Environment Prompt Snapshots

These fixtures store rendered runtime-prompt content for the environment
templates under `src/vibesys/orchestration/prompts/environments/<kind>/`. They are grouped
by:

```text
<kind>/<case>/<template>.md
```

`kind` is the run environment (`modal`, `docker`); `template` matches the
`.j2` file that produced it (`runtime_notes` or `prompt_notes`).

When a prompt change is intentional, regenerate with
`UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest
tests/vibesys/sandbox/test_environment_prompt_snapshots.py` and review the fixture
diff as the prompt diff. Do not blindly accept a regenerated snapshot: these
files show exactly what an agent will see.
