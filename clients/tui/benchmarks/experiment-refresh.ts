import type {HypothesisEntry} from '@vibesys/backend-client';
import {
  initialSessionState,
  mergeExperimentEntries,
  openExperimentLog,
  setExperiments,
} from '../src/session-model.js';

const sizes = [20, 100, 500] as const;
const samples = 2_000;
const batches = 7;

function entry(index: number): HypothesisEntry {
  const firstRound = (index - 1) * 3 + 1;
  return {
    hypothesis_id: `H-${String(index).padStart(4, '0')}`,
    title: `Hypothesis ${index}`,
    claim: `Claim ${index}`,
    action: `Measure hypothesis ${index}`,
    first_round: firstRound,
    last_round: firstRound + 2,
    rounds: Array.from({length: 3}, (_, offset) => ({
      round: firstRound + offset,
      passed: true,
      reviewed: true,
    })),
  };
}

console.log('| hypotheses | client delta application ms |');
console.log('| ---: | ---: |');
for (const count of sizes) {
  const entries = Array.from({length: count}, (_, index) => entry(index + 1));
  const timings: number[] = [];
  for (let batch = 0; batch < batches; batch += 1) {
    let state = setExperiments(openExperimentLog(initialSessionState()), entries);
    const started = performance.now();
    for (let sample = 0; sample < samples; sample += 1) {
      const previous = entries[count - 1];
      const round = count * 3 + sample + 1;
      const replacement = {
        ...previous,
        last_round: round,
        rounds: [...previous.rounds, {round, passed: sample % 2 === 0, reviewed: true}],
      };
      const merged = mergeExperimentEntries(state.experimentLog?.entries ?? [], [replacement], []);
      state = setExperiments(state, merged);
    }
    timings.push((performance.now() - started) / samples);
  }
  timings.sort((left, right) => left - right);
  console.log(`| ${count} | ${timings[Math.floor(timings.length / 2)].toFixed(4)} |`);
}
