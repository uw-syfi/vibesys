import {scopeStateForRound} from './experiments.js';
import {
  iterationRailRounds,
  type SessionState,
  visibleIterationId,
  visiblePhases,
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
  const rows = iterationRailRounds(state);
  const visible = visibleIterationId(state);
  if (rows.length === 0) return state;
  const index =
    visible === null ? (delta > 0 ? -1 : 0) : rows.findIndex(row => row.iterationId === visible);
  const selected = rows[(index + delta + rows.length) % rows.length];
  return selected === undefined ? state : withSelectedIteration(state, selected);
}

export function selectNextRound(state: SessionState): SessionState {
  return moveRound(state, 1);
}

export function selectPreviousRound(state: SessionState): SessionState {
  return moveRound(state, -1);
}

export function selectRound(state: SessionState, roundNumber: number): SessionState {
  const selected = iterationRailRounds(state).find(row => row.number === roundNumber);
  return selected === undefined ? state : withSelectedIteration(state, selected);
}

export function selectIteration(state: SessionState, iterationId: string): SessionState {
  const selected = iterationRailRounds(state).find(row => row.iterationId === iterationId);
  return selected === undefined ? state : withSelectedIteration(state, selected);
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

function withSelectedIteration(
  state: SessionState,
  selected: ReturnType<typeof iterationRailRounds>[number],
): SessionState {
  const roundNumber = selected.number;
  return {
    ...state,
    ...(state.hypothesisScope !== null && roundNumber !== null
      ? scopeStateForRound(state, roundNumber)
      : {}),
    selectedIterationId: selected.iterationId,
    selectedRound: roundNumber,
    selectedAgentKind: null,
    selectedEntryId: null,
    overlay: null,
  };
}
