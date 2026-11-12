Accuracy checker for Qwen3.6-35B-A3B (service-style), shared by every task.

`checker.py` here is the canonical source. Each task carries a byte-for-byte
copy as its own `checker.py`, so the checker is part of the task's version and
of its task image, never read from a candidate's checkout; the example tests
fail when a copy drifts. Edit this file, then copy it into every task.

The checker drives a **running** OpenAI-compatible server over HTTP. It does
not import the candidate's model or load weights locally, so it works the same
against any server it can reach. Point it at the server URL:

    python checker.py --url http://localhost:8000
    python checker.py --url http://localhost:8000 --seed 0

Correctness is established with three reference-free gates (see `--help` for
thresholds):

1. **Sentinel-echo** -- each request embeds a random token the prompt tells the
   model to reproduce; canned/templated servers can't reproduce a fresh token.
2. **Known-answer** -- near-deterministic factual prompts at temperature 0.
3. **Greedy determinism** -- identical greedy requests must return identical
   text, so a real prompt-conditioned MoE forward pass is required.

Exit code is 0 only when every gate clears its rate threshold and no transport
error occurred.
