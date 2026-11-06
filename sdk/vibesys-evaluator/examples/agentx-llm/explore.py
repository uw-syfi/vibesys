"""agentx-llm: an exploratory replay, for a profiler's capture or an experiment in a lab.

Run in the task's image beside the candidate's server, as a vibesys-cli lab
holds them (``vibesys-cli lab exec <lab> --in task -- python explore.py``): it
replays the task's corpus with the load given here and prints what it saw.
Nothing it prints is a measurement; only ``bench.py benchmark`` scores.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench import MODEL, REVISION

from vibesys_evaluator.llm_serving import agentx, endpoint

if __name__ == "__main__":
    values = agentx.explore(
        model=MODEL,
        tokenizer=MODEL,
        tokenizer_revision=REVISION,
        concurrency=16,
        max_context_length=262144,
        artifacts=Path("/vibesys/artifacts/agentx-explore"),
        duration_secs=120,
        warmup_requests_per_lane=2,
        dataset_entries=8,
        url=endpoint(),
    )
    print(json.dumps(values, sort_keys=True))
