import {scopeStateForRound} from './experiments.js';
import {
  type SessionState,
  stripRounds,
  visiblePhases,
  visibleRoundNumber,
} from './session-model.js';

/** Pure navigation transitions for the selected round and agent. */
export function selectNextAgent(state: SessionState): SessionState {
  const phases = visiblePhases(state);
  if (phases.length === 0) return state;
  const index =
    state.selectedAgentKind === null
      ? -1
      : phases.findIndex(phase => phase.kind === state.selectedAgentKind);
  return {
    ...state,
    selectedAgentKind: phases[(index + 1 + phases.length) % phases.length]?.kind ?? null,
    roundFocus: 'agents',
    overlay: null,
  };
}

export function selectPreviousAgent(state: SessionState): SessionState {
  const phases = visiblePhases(state);
  if (phases.length === 0) return state;
  const index =
    state.selectedAgentKind === null
      ? 0
      : phases.findIndex(phase => phase.kind === state.selectedAgentKind);
  return {
    ...state,
    selectedAgentKind: phases[(index - 1 + phases.length) % phases.length]?.kind ?? null,
    roundFocus: 'agents',
    overlay: null,
  };
}

function moveRound(state: SessionState, delta: number): SessionState {
  const rounds = stripRounds(state);
  const visible = visibleRoundNumber(state);
  if (rounds.length === 0) return state;
  const index =
    visible === null ? (delta > 0 ? -1 : 0) : rounds.findIndex(round => round.number === visible);
  return withSelectedRound(
    state,
    rounds[(index + delta + rounds.length) % rounds.length]?.number ?? null,
  );
}

export function selectNextRound(state: SessionState): SessionState {
  return moveRound(state, 1);
}

export function selectPreviousRound(state: SessionState): SessionState {
  return moveRound(state, -1);
}

export function selectRound(state: SessionState, roundNumber: number): SessionState {
  const rounds = stripRounds(state);
  return rounds.some(round => round.number === roundNumber)
    ? withSelectedRound(state, roundNumber)
    : state;
}

export function clearAgentSelection(state: SessionState): SessionState {
  return {
    ...state,
    selectedAgentKind: null,
    selectedEntryId: null,
    roundFocus: 'transcript',
    overlay: null,
  };
}

export function selectAgent(state: SessionState, kind: string): SessionState {
  return {
    ...state,
    selectedAgentKind: state.selectedAgentKind === kind ? null : kind,
    selectedEntryId: null,
    roundFocus: 'agents',
    overlay: null,
  };
}

function withSelectedRound(state: SessionState, roundNumber: number | null): SessionState {
  return {
    ...state,
    ...(state.hypothesisScope !== null && roundNumber !== null
      ? scopeStateForRound(state, roundNumber)
      : {}),
    selectedRound: roundNumber,
    selectedAgentKind: null,
    selectedEntryId: null,
    overlay: null,
  };
}
