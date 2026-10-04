import type {HypothesisEntry, HypothesisRound} from '@vibesys/backend-client';
import type {RoundState} from './run-map.js';

export type RoundOutcome = 'done' | 'fail' | 'skipped' | 'live' | 'planned';

/** One run-map round joined to its numeric experiment owner, when it has one. */
export interface RoundExperiment {
  round: RoundState;
  hypothesis: HypothesisEntry | null;
  record: HypothesisRound | null;
}

export interface RoundExperimentOwner {
  hypothesis: HypothesisEntry;
  record: HypothesisRound | null;
}

/**
 * Includes the backend's planned numeric rounds without dropping unnumbered
 * fallback rounds. Label-keyed rounds remain in first-observed fold order.
 */
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

/** Ordered numeric round identities claimed by a hypothesis. */
export function hypothesisRoundNumbers(entry: HypothesisEntry): number[] {
  const listed = (entry.rounds ?? []).map(round => round.round);
  if (listed.length > 0) return [...listed].sort((left, right) => left - right);
  if (entry.first_round <= 0 || entry.last_round < entry.first_round) return [];
  return Array.from(
    {length: entry.last_round - entry.first_round + 1},
    (_, index) => entry.first_round + index,
  );
}

/**
 * Joins run-map rounds to experiment ownership once, at the owning package.
 * Unnumbered rounds remain in the result with no fabricated experiment owner.
 */
export function joinRoundsWithExperiments(
  rounds: readonly RoundState[],
  entries: readonly HypothesisEntry[],
): RoundExperiment[] {
  const owners = experimentOwners(entries);
  return rounds.map(round => {
    const owner = round.number === null ? null : (owners.get(round.number) ?? null);
    return {
      round,
      hypothesis: owner?.hypothesis ?? null,
      record: owner?.record ?? null,
    };
  });
}

/** Experiment ownership for one numeric round, independent of a run-map row. */
export function experimentForRound(
  entries: readonly HypothesisEntry[],
  roundNumber: number | null,
): RoundExperimentOwner | null {
  if (roundNumber === null) return null;
  const owner = experimentOwners(entries).get(roundNumber);
  return owner === undefined ? null : {hypothesis: owner.hypothesis, record: owner.record};
}

/** Outcome precedence shared by frontends. */
export function roundOutcome(round: RoundState, record: HypothesisRound | null): RoundOutcome {
  switch (round.status) {
    case 'active':
      return 'live';
    case 'planned':
      return 'planned';
    case 'failed':
      return 'fail';
    case 'completed':
      if (record?.judge_verdict === 'fail') return 'fail';
      return round.profileSkipped === true ? 'skipped' : 'done';
  }
}

interface ExperimentOwner {
  hypothesis: HypothesisEntry;
  record: HypothesisRound | null;
}

function experimentOwners(entries: readonly HypothesisEntry[]): Map<number, ExperimentOwner> {
  const owners = new Map<number, ExperimentOwner>();
  for (const hypothesis of entries) {
    const records = new Map((hypothesis.rounds ?? []).map(record => [record.round, record]));
    for (const number of hypothesisRoundNumbers(hypothesis)) {
      owners.set(number, {hypothesis, record: records.get(number) ?? null});
    }
  }
  return owners;
}
