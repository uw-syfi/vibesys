# Evolve Prompt Snapshots

These fixtures store final rendered evolve prompts after modality includes,
domain-role interpolation, and cold-start/offspring branches have run. They are
grouped as:

```text
<domain>/<cold-start|offspring>/<mutator|judge|profiler>.md
```

The matrix covers the two commonly used domain shapes: a generic in-process
native workload with no modality, and an LLM-serving text-generation workload.

When a prompt change is intentional, regenerate only the affected fixtures and
review their diffs. Do not blindly accept regenerated snapshots: these files
show exactly what each agent role will see.
