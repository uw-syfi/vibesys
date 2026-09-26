# Experiment Progress

## Iter 1 — Implementer on issue #1

**Issue**: [feature] Initial task: Maximize tok/s throughput.

**Summary**: Built the inference server.

**Files touched**:
- `server.py`

**Self-check**: ran the accuracy checker locally

### Iter 1 — Judge on issue #1

**Verdict**: PASS

**Analysis**: reviewed the diff and the accuracy checks

## Iter 1 — Performance Evaluator

**Throughput trend**: IMPROVED

**Latency trend**: MIXED

**Analysis**: Throughput saturates around rate=8; TTFT stays flat below that.

**Notes for next perf evaluator**:
- rate=8 is the saturation point; try rate=16 next iteration

