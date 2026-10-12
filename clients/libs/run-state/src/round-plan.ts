import type {RoundState} from './run-map.js';

/** Include configured future numeric rounds without dropping observed label-keyed rounds. */
export function roundsWithPlan(
  rounds: readonly RoundState[],
  maxRounds: number | null,
): RoundState[] {
  const numeric = rounds.flatMap(round =>
    round.key.kind === 'number' ? [{round, number: round.key.number}] : [],
  );
  const unnumbered = rounds.filter(round => round.key.kind === 'label');
  const highest = Math.max(maxRounds ?? 0, ...numeric.map(item => item.number), 0);
  const known = new Map(numeric.map(item => [item.number, item.round]));
  const planned: RoundState[] = Array.from({length: highest}, (_, index) => {
    const number = index + 1;
    return known.get(number) ?? {key: {kind: 'number' as const, number}, number, status: 'planned'};
  });
  return [...planned, ...unnumbered];
}
