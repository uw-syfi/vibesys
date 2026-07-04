Accuracy checker inputs for Show-o2 1.5B HQ.

Run:

```bash
python checker.py --url http://localhost:8000 --steps 1
```

This checker performs an HTTP image-generation smoke test and verifies that the
server returns PNG bytes. It is intentionally output-agnostic because diffusion
sampling is not token-exact.
