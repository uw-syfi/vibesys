# Prompt Snapshots

These fixtures store final rendered agent prompts after template includes,
domain interpolation, and conditional Jinja branches have run. They are grouped
by:

```text
<domain>/<context>/<role>.md
```

Contexts:

- `full`: benchmark path, accuracy checker path, and runtime notes are present.
- `minimal`: optional benchmark/checker paths and runtime notes are absent.

When a prompt change is intentional, update the relevant fixture and review the
fixture diff as the prompt diff. Do not blindly accept regenerated snapshots:
these files show exactly what an agent will see.
