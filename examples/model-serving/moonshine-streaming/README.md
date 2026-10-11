# moonshine-streaming

Streaming ASR serving with `moonshine-streaming-medium`: clients send 16 kHz mono PCM audio
in chunks, and the server emits partial transcripts per chunk and a final transcript on
finalize. The benchmark drives concurrent clients in real-time pacing and measures TTFT,
TPOT, and audio-seconds-per-wall-second.

One project, two tasks under `.vibesys/tasks/`. They share the evaluator (accuracy checker,
benchmark, reference, test audio) and differ only in the objective and the starting point:

| Task | Starting point |
| --- | --- |
| `from-scratch` | The agent writes the serving system itself. |
| `vllm` | The workspace holds a pinned vLLM checkout (`vllm/`) to start from. |

Copy the project with `vibesys example new model-serving/moonshine-streaming DEST`, then run one
task from `DEST` (each task has its own README):

```bash
vibesys --runs-dir /work/vibesys-runs --local --task from-scratch
vibesys --runs-dir /work/vibesys-runs --local --task vllm
```

`reference_inference.py` is a copy of the HF reference model code kept at the project root.
