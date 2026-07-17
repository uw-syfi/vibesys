import {describe, expect, it} from 'vitest';
import type {RunEvent} from './protocol.js';
import {applyRunMapEvent, type RunMapState, roundAgentElapsedMs} from './run-map.js';

describe('run map round timing', () => {
  it('stops counting when an agent phase finishes', () => {
    let state = initialRunMapState();
    state = applyRunMapEvent(state, phaseEvent(1, 'phase_started', '2026-01-01T00:00:00Z'));
    state = applyRunMapEvent(state, phaseEvent(2, 'phase_finished', '2026-01-01T00:00:05Z'));

    expect(roundAgentElapsedMs(onlyRound(state), new Date('2026-01-01T00:01:00Z'))).toBe(5000);
  });

  it('closes a phase by agent role when the exact invocation key is absent', () => {
    let state = initialRunMapState();
    state = applyRunMapEvent(
      state,
      phaseEvent(1, 'phase_started', '2026-01-01T00:00:00Z', undefined),
    );
    state = applyRunMapEvent(
      state,
      phaseEvent(2, 'phase_finished', '2026-01-01T00:00:05Z', 'finish-only'),
    );

    expect(state.rounds[0]?.activeAgentStarts).toEqual({});
    expect(roundAgentElapsedMs(onlyRound(state), new Date('2026-01-01T00:01:00Z'))).toBe(5000);
  });
});

function initialRunMapState(): RunMapState {
  return {outerLoop: null, rounds: [], phases: []};
}

function onlyRound(state: RunMapState) {
  expect(state.rounds).toHaveLength(1);
  return state.rounds[0] as NonNullable<(typeof state.rounds)[0]>;
}

function phaseEvent(
  sequence: number,
  type: 'phase_started' | 'phase_finished',
  timestamp: string,
  invocationId = 'invocation-1',
): RunEvent {
  return {
    sequence,
    timestamp,
    type,
    round_label: 'round-1',
    agent_kind: 'implementer',
    ...(invocationId === undefined ? {} : {invocation_id: invocationId}),
  };
}
