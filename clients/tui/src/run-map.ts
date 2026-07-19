import type {RunEvent} from './protocol.js';
import {
  activeTimingElapsedMs,
  closeActiveAgentTimings,
  finishAgentTiming,
  type RoundTimingState,
  startAgentTiming,
} from './round-timing.js';

export type AgentPhaseStatus = 'pending' | 'active' | 'completed' | 'failed';
export type RoundStatus = 'active' | 'completed' | 'failed';

export interface RoundSummary extends RoundTimingState {
  number: number;
  status: RoundStatus;
  startedAt?: string;
  finishedAt?: string;
}

export interface AgentPhase {
  kind: string;
  status: AgentPhaseStatus;
  roundNumber: number | null;
  roundLabel: string | null;
  invocationId?: string;
  startedAt?: string;
  finishedAt?: string;
}

export interface RunMapState {
  outerLoop: string | null;
  rounds: RoundSummary[];
  phases: AgentPhase[];
}

export function applyRunMapEvent(state: RunMapState, event: RunEvent): RunMapState {
  const outerLoop =
    event.type === 'run_started' && event.data?.kind === 'run_started'
      ? event.data.outer_loop
      : state.outerLoop;
  const rounds = applyRoundEvent(state.rounds, event);
  const phases = applyPhaseEvent({...state, outerLoop, rounds}, event);
  return {outerLoop, rounds, phases};
}

export function visibleRoundNumber(
  rounds: RoundSummary[],
  selectedRound: number | null,
): number | null {
  if (selectedRound !== null) return selectedRound;
  const active = [...rounds].reverse().find(round => round.status === 'active');
  if (active) return active.number;
  return rounds.at(-1)?.number ?? null;
}

export function visiblePhases(phases: AgentPhase[], roundNumber: number | null): AgentPhase[] {
  return phases.filter(phase => phase.roundNumber === roundNumber);
}

export function roundNumberFromLabel(label: string | null | undefined): number | null {
  if (!label) return null;
  const match = label.match(/(?:round|iter(?:ation)?)\D*(\d+)/i);
  return match ? Number(match[1]) : null;
}

function applyPhaseEvent(state: RunMapState, event: RunEvent): AgentPhase[] {
  const kind = event.agent_kind;
  if (!kind) return state.phases;
  const roundNumber = roundNumberFromLabel(event.round_label);
  let phases = state.phases;
  if (roundNumber !== null && state.outerLoop !== null) {
    phases = seedExpectedPhases(state.outerLoop, phases, roundNumber);
  }
  if (event.type !== 'phase_started' && event.type !== 'phase_finished') {
    return ensurePhase(phases, kind, roundNumber);
  }
  const status =
    event.type === 'phase_started' ? 'active' : event.status === 'failed' ? 'failed' : 'completed';
  return upsertPhase(phases, {
    kind,
    status,
    roundNumber,
    roundLabel: event.round_label ?? null,
    ...(event.invocation_id ? {invocationId: event.invocation_id} : {}),
    ...(event.type === 'phase_started'
      ? {startedAt: event.timestamp}
      : {finishedAt: event.timestamp}),
  });
}

function applyRoundEvent(rounds: RoundSummary[], event: RunEvent): RoundSummary[] {
  const number = roundNumberFromLabel(event.round_label);
  if (number === null) return rounds;
  const existing = rounds.find(round => round.number === number);
  const status =
    event.type === 'round_finished'
      ? event.status === 'failed'
        ? 'failed'
        : 'completed'
      : 'active';
  const patch: RoundSummary = {
    number,
    status,
    ...(event.type === 'round_finished'
      ? {finishedAt: event.timestamp}
      : {startedAt: event.timestamp}),
  };
  const round = existing ? mergeRound(existing, patch) : patch;
  return replaceRound(rounds, updateRoundAgentElapsed(round, event));
}

function seedExpectedPhases(
  outerLoop: string,
  current: AgentPhase[],
  roundNumber: number,
): AgentPhase[] {
  const expected = expectedRoles(outerLoop);
  let phases = current;
  for (const kind of expected) phases = ensurePhase(phases, kind, roundNumber);
  return phases;
}

function expectedRoles(outerLoop: string): string[] {
  if (outerLoop === 'agent') return ['orchestrator', 'implementer', 'judge', 'profiler'];
  if (outerLoop === 'plain') return ['implementer', 'judge', 'perf_eval'];
  if (outerLoop === 'evolve') {
    return ['implementer', 'judge', 'profiler'];
  }
  return [];
}

function ensurePhase(phases: AgentPhase[], kind: string, roundNumber: number | null): AgentPhase[] {
  if (phases.some(phase => phase.kind === kind && phase.roundNumber === roundNumber)) {
    return phases;
  }
  return [...phases, {kind, status: 'pending', roundNumber, roundLabel: null}];
}

function upsertPhase(phases: AgentPhase[], patch: AgentPhase): AgentPhase[] {
  const existing = phases.findIndex(
    phase => phase.kind === patch.kind && phase.roundNumber === patch.roundNumber,
  );
  if (existing === -1) return [...phases, patch];
  return phases.map((phase, index) =>
    index === existing
      ? {
          ...phase,
          ...patch,
          ...((patch.startedAt ?? phase.startedAt)
            ? {startedAt: patch.startedAt ?? phase.startedAt}
            : {}),
          ...((patch.finishedAt ?? phase.finishedAt)
            ? {finishedAt: patch.finishedAt ?? phase.finishedAt}
            : {}),
        }
      : phase,
  );
}

function replaceRound(rounds: RoundSummary[], round: RoundSummary): RoundSummary[] {
  const existing = rounds.findIndex(item => item.number === round.number);
  if (existing === -1) return [...rounds, round].sort((a, b) => a.number - b.number);
  return rounds.map((item, index) => (index === existing ? round : item));
}

function mergeRound(round: RoundSummary, patch: RoundSummary): RoundSummary {
  const startedAt = earliestTimestamp(round.startedAt, patch.startedAt);
  return {
    ...round,
    ...patch,
    ...(startedAt ? {startedAt} : {}),
    ...((patch.finishedAt ?? round.finishedAt)
      ? {finishedAt: patch.finishedAt ?? round.finishedAt}
      : {}),
    ...((patch.agentIntervals ?? round.agentIntervals)
      ? {agentIntervals: patch.agentIntervals ?? round.agentIntervals}
      : {}),
    ...((patch.activeAgentStarts ?? round.activeAgentStarts)
      ? {activeAgentStarts: patch.activeAgentStarts ?? round.activeAgentStarts}
      : {}),
  };
}

function earliestTimestamp(
  left: string | undefined,
  right: string | undefined,
): string | undefined {
  if (!left) return right;
  if (!right) return left;
  return new Date(right).getTime() < new Date(left).getTime() ? right : left;
}

function updateRoundAgentElapsed(round: RoundSummary, event: RunEvent): RoundSummary {
  if (event.type !== 'phase_started' && event.type !== 'phase_finished') {
    if (event.type !== 'round_finished') return round;
    return closeActiveAgentTimings(round, event.timestamp);
  }
  return event.type === 'phase_started'
    ? startAgentTiming(round, event)
    : finishAgentTiming(round, event);
}

export function roundAgentElapsedMs(round: RoundSummary, now = new Date()): number {
  return activeTimingElapsedMs(round, now);
}
