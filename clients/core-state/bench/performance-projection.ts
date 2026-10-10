import type {HypothesisEntry, RunEvent} from '@vibesys/backend-client';
import {projectPerformance} from '../src/performance-projection.js';

const ROUNDS = 20_000;
const PASSES = 20;
const events: RunEvent[] = [];
const experiments: HypothesisEntry[] = [];

for (let round = 1; round <= ROUNDS; round += 1) {
  events.push({
    sequence: round,
    timestamp: new Date(1_767_225_600_000 + round).toISOString(),
    type: 'gate_finished',
    status: 'completed',
    round_label: `round-${round}`,
    data: {
      kind: 'gate_finished',
      gate: 'benchmark',
      metric: 'tokens_per_second',
      value: 10_000 + round,
      unit: 'tok/s',
    },
  });
  experiments.push({
    hypothesis_id: `H-${round}`,
    first_round: round,
    last_round: round,
    perf_direction: 'max',
    perf_delta_pct: round / 100,
  });
}

let pointCount = 0;
const startedAt = performance.now();
for (let pass = 0; pass < PASSES; pass += 1) {
  pointCount = projectPerformance({events, experiments}).points.length;
}
const totalMs = performance.now() - startedAt;

if (pointCount !== ROUNDS) {
  throw new Error(`projected ${pointCount} points from ${ROUNDS} benchmark rounds`);
}

console.log('| Rounds | Passes | Total ms | ms/projection | ns/input row |');
console.log('|---:|---:|---:|---:|---:|');
console.log(
  `| ${ROUNDS} | ${PASSES} | ${totalMs.toFixed(2)} | ${(totalMs / PASSES).toFixed(3)} | ${((totalMs * 1_000_000) / (ROUNDS * PASSES * 2)).toFixed(1)} |`,
);
