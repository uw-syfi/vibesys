Accuracy checker for Llama-3-8B (service-style).

The checker drives a **running** OpenAI-compatible server over HTTP — it does
not import the candidate's model or load weights locally, so it works the same
against a local, Docker, or remote Modal server. Point it at the server URL:

```
python checker.py --url http://localhost:8000
python checker.py --url https://<app>.modal.run --seed 0
```

Because there is no local GPU reference to diff against, correctness is
established with three reference-free gates (see `--help` for thresholds):

1. **Sentinel-echo** — each request embeds a random token the prompt tells the
   model to reproduce; canned/templated servers can't reproduce a fresh token.
2. **Known-answer** — near-deterministic factual prompts at temperature 0
   (capital of France → Paris, 1+1 → 2, …); a prompt echoer fails these.
3. **Greedy determinism** — the same prompt twice at temperature 0 must match.

Exit code 0 iff there are no transport errors and all three gates clear their
thresholds. Use `--output-json <path>` to dump per-request detail.
