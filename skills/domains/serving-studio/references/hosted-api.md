# The ServingStudio public API

ServingStudio documents its own API in three skills that live in its repository, under
`public_api/skills/` of ServingStudioSim. Read the one your question needs, at the revision the
endpoint serves (the `sim_commit` that `servingstudio.py health` prints), from a local checkout
(`$SERVINGSTUDIO_SIM/public_api/skills/<name>/SKILL.md`) or from GitHub:

```
https://raw.githubusercontent.com/SyFI-ServingStudio/ServingStudioSim/<sim_commit>/public_api/skills/<name>/SKILL.md
```

| Question | Upstream skill |
| --- | --- |
| Throughput, TTFT, TPOT and latency of a deployment under a request workload | `servingstudio-simulate` |
| How long one iteration of given batch shapes takes, split by kernel | `servingstudio-timing-predict` |
| Measured time of one kernel at given shapes, and the hardware's peaks | `servingstudio-kernel-performance` |

They are not copied here: they are ServingStudio's to change, and their license is not this
repository's. The API also describes itself at `$SERVINGSTUDIO_API/openapi.json`.

## What this skill adds

- **Run simulations through the wrapper.** It waits within the service's limits and keeps the
  provenance a frozen prediction needs:

  ```bash
  python3 <skill-dir>/scripts/servingstudio.py simulate --request req.json --out <scratch>/sim-<name>
  ```

  `req.json` is the body of `POST /simulate` as `servingstudio-simulate` describes it. The
  directory then holds `request.json`, `record.json` (the final record, with `summary`) and
  `provenance.json`. Exit 0 means the run is `done` with every request finished; 1 that it
  failed, timed out or left requests unfinished; 2 that the service refused the request or
  stopped answering (`submission-error.json` keeps a refusal's answer); 3 that the run finished
  but the service's revision changed or could not be read around it, so the result cannot be
  frozen as that revision's.
- **Translate a closed-loop benchmark exactly.** `vllm bench serve` with a random dataset,
  N prompts, fixed lengths and `--max-concurrency C` is N independent requests, all ready at
  once, at most C in flight across every replica together:

  ```json
  {"source": "generated",
   "generator": {"type": "synthetic", "sessions": 64, "rounds": "1",
                 "input_len": "1024", "output_len": "128", "arrival_rate": 1, "seed": 1},
   "load": {"concurrency": 16}}
  ```

  Its output tokens per second compares with `summary.throughput.decode_tok_s`, which counts
  the whole run up to the last request's end, as the benchmark does. The wrapper refuses a run
  whose `summary.requests.finished` is below its total.
- **Use the benchmark's workload.** Upload the task's own arrivals and lengths as a CSV, or a
  generator whose arguments reproduce them; a capture recorded on another workload answers
  another question. For a speculative member, give the acceptance the engine measured.
- **Reproduce before predicting.** The first simulation of a plan is the head's configuration;
  its residual against the measured value goes with every number after it.

## Limits

Two simulations run at a time per service, each at most 10 minutes and 2,000 requests, and
`POST /simulate` takes six requests a minute per client. Results are kept 24 hours, so copy what
you cite into your scratch directory (the wrapper does). The hosted service offers published GPU
presets only and no what-if overrides; for those, use a local checkout
([local-sim.md](local-sim.md)).
