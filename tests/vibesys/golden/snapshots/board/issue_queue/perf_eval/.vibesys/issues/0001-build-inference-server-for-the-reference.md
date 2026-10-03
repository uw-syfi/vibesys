# #0001 - Build inference server for the reference model

- **Type**: feature
- **Status**: closed
- **Attempts**: 1

## Description

## Background

Build a production-ready inference server for the reference implementation at
`reference/model.py`. Inspect the reference and checker rather than
substituting a generic model wrapper. Keep all candidate work in this workspace.

Objective: Maximize token throughput without losing correctness.

Use CUDA with bfloat16.

## Acceptance criteria

- Implement the model and its weight loading from the provided local inputs.
- Expose `/v1/completions` streaming non-empty token deltas and `/health`.
- Preserve the checker-required `VibeServeModel.from_pretrained` and `generate` interface.
- Add pytest coverage for health, completion streaming, accuracy, and a benchmark smoke run.
- Run the configured checks and leave a reproducible candidate.

## Commands

Accuracy command: uv run check-accuracy
Benchmark command: uv run benchmark

## Timeline

- `<TIMESTAMP>` **loop:bootstrap** create (iteration 1)
- `<TIMESTAMP>` **loop** open->in_progress (iteration 1): claimed for processing
- `<TIMESTAMP>` **implementer** attempt (iteration 1): Built the inference server.
- **Summary**: Built the inference server.
- **Self Check**: Ran the accuracy checker locally.
- **Files touched**: `server.py`
- `<TIMESTAMP>` **judge** in_progress->closed (iteration 1): closed by judge after attempt 1
- **Verdict**: pass
- **Analysis**: Reviewed the diff and the accuracy checks.
