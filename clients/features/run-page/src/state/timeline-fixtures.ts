/** Candidate, attempt and step builders for the Timeline and idea tests: plain mocked run data. */
import type {
  Candidate,
  CandidateAttempt,
  CandidateOutcome,
  CandidateStep,
  CandidateTree,
} from '@vibesys/run-state/candidates';
import type {RunModel} from './run-model.js';

export function step(span: string, kind: string, role: string | null, at: number, took: number) {
  return {
    span,
    kind,
    title: `${kind} ${span}`,
    role,
    status: 'done',
    startedAt: at,
    endedAt: at + took,
  } satisfies CandidateStep;
}

export function attempt(id: string, outcome: CandidateOutcome, at: number, took: number) {
  return {
    span: id,
    title: id,
    idea: null,
    round: null,
    outcome,
    reason: outcome === 'failed' ? 'turned_down' : null,
    revision: null,
    baseRound: null,
    startedAt: at,
    endedAt: outcome === 'running' ? null : at + took,
    measurement:
      outcome === 'passed'
        ? {
            span: `${id}-g`,
            at: at + took,
            metricValue: 40,
            vsBaseline: 1.5,
            against: null,
            readings: [],
            inputReadings: [],
            verdict: 'passed',
            failed: null,
          }
        : null,
    steps: [
      step(`${id}-t`, 'agent_turn', 'implementer', at, took / 3),
      step(`${id}-g`, 'gate', null, at + took / 3, took / 3),
      step(`${id}-j`, 'agent_turn', 'judge', at + (2 * took) / 3, took / 3),
    ],
  } satisfies CandidateAttempt;
}

export function idea(id: string, outcome: CandidateOutcome, at: number, took: number): Candidate {
  const tried = attempt(`${id}-a`, outcome, at, took);
  return {
    id,
    hypothesis: null,
    parent: null,
    title: id,
    idea: null,
    round: null,
    outcome,
    reason: tried.reason,
    baseRound: null,
    measurement: tried.measurement,
    attempts: [tried],
    startedAt: at,
    endedAt: tried.endedAt,
    running: [],
  };
}

export function modelOf(
  candidates: readonly Candidate[],
  best: CandidateTree['best'] = null,
): RunModel {
  const tree: CandidateTree = {
    candidates,
    best,
    adoptionOpened: best !== null,
    bestPath: best?.candidate ? [best.candidate] : [],
  };
  return {candidates: tree, direction: 'max', pauses: [], baselines: []} as unknown as RunModel;
}
