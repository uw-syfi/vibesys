Accuracy checker for GLM-5.2 (service-style).

The checker drives a **running** OpenAI-compatible server over HTTP. It does
not import the candidate's model or load weights locally, so it works the same
against a local, Docker, or remote Modal server. Point it at the server URL:

    python checker.py --url http://localhost:8000
    python checker.py --url https://<app>.modal.run --seed 0

Correctness is established with three reference-free gates (see `--help` for
thresholds):

1. **Sentinel-echo** -- each request embeds a random token the prompt tells the
   model to reproduce; canned/templated servers can't reproduce a fresh token.
2. **Known-answer** -- near-deterministic factual prompts at temperature 0.
3. **Greedy determinism** -- identical greedy requests must return identical
   text, so a real prompt-conditioned MoE forward pass is required.

Exit code is 0 only when every gate clears its rate threshold and no transport
error occurred.
