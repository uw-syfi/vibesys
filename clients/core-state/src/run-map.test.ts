import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/backend-client';
import {hasActiveAgentTiming} from './round-timing.js';
import {
  applyRunMapEvent,
  expectedRolesForSeeding,
  type RunMapState,
  roundAgentElapsedMs,
} from './run-map.js';

describe('run map projection', () => {
  it('tracks concurrent same-role executions independently', () => {
    let state = emptyRunMap();
    state = applyRunMapEvent(state, execution(1, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'b'));
    state = applyRunMapEvent(state, execution(3, 'agent_execution_finished', 'a'));

    expect(state.phases.filter(phase => phase.kind === 'implementer')).toMatchObject([
      {executionId: 'a', status: 'completed'},
      {executionId: 'b', status: 'active'},
    ]);
  });

  it('does not double count compatibility phase events', () => {
    let state = emptyRunMap();
    state = applyRunMapEvent(state, execution(1, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, execution(2, 'phase_started', 'a'));
    state = applyRunMapEvent(state, execution(3, 'agent_execution_finished', 'a'));
    state = applyRunMapEvent(state, execution(4, 'phase_finished', 'a'));

    const round = requiredRound(state);
    expect(roundAgentElapsedMs(round, new Date('2026-01-01T00:00:05Z'))).toBe(2000);
  });

  it('captures the agent runtime identity on start and keeps it after finish', () => {
    let state = applyRunMapEvent(emptyRunMap(), {
      ...execution(1, 'agent_execution_started', 'a'),
      data: {
        kind: 'agent_execution_started',
        stage: 'implementation',
        attempt: 1,
        system_prompt: '',
        user_prompt: 'Implement',
        activity: {
          kind: 'agent_execution_activity_changed',
          mode: 'thinking',
          summary: 'Starting',
          tool: null,
        },
        driver: 'agentshim',
        provider: 'codex',
        model: 'gpt-5.1-codex-max',
      },
    });
    state = applyRunMapEvent(state, execution(2, 'agent_execution_finished', 'a'));

    expect(state.phases.filter(phase => phase.kind === 'implementer')).toMatchObject([
      {
        executionId: 'a',
        status: 'completed',
        driver: 'agentshim',
        provider: 'codex',
        model: 'gpt-5.1-codex-max',
      },
    ]);
  });

  it('defaults the runtime identity to null when the event omits it', () => {
    const state = applyRunMapEvent(emptyRunMap(), execution(1, 'agent_execution_started', 'a'));

    expect(state.phases.filter(phase => phase.kind === 'implementer')).toMatchObject([
      {driver: null, provider: null, model: null},
    ]);
  });

  it('closes active timing when a run is interrupted, with no agent scope on the event', () => {
    // The three backends that emit this event (`runtime.py` on SIGTERM,
    // `controller.py`, `headless.py`) all describe the run, not an agent, so
    // the event carries no `agent_kind` and no `round_label`. A fold that reads
    // the scope first drops it and the round's clock never stops.
    let state = applyRunMapEvent(emptyRunMap(), execution(1, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, runInterrupted(4));

    const round = requiredRound(state);
    expect(round.status).toBe('failed');
    expect(hasActiveAgentTiming(round)).toBe(false);
    // Frozen where the run died: the same value ten seconds later.
    expect(roundAgentElapsedMs(round, new Date('2026-01-01T00:00:10Z'))).toBe(3000);
    expect(roundAgentElapsedMs(round, new Date('2026-01-01T00:10:00Z'))).toBe(3000);
  });

  it('publishes ordinary array behavior from persistent run-map storage', () => {
    let state = applyRunMapEvent(initialRunMap(), runStarted('agent'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, execution(3, 'agent_execution_finished', 'a'));

    expect(Array.isArray(state.rounds)).toBe(true);
    expect(Array.isArray(state.phases)).toBe(true);
    expect(state.rounds).toMatchObject([{number: 1, status: 'active'}]);
    expect([...state.rounds]).toEqual(state.rounds);
    expect(state.phases.map(phase => phase.kind)).toEqual([
      'orchestrator',
      'implementer',
      'judge',
      'profiler',
    ]);
    expect(state.phases.findIndex(phase => phase.executionId === 'a')).toBe(1);
    expect(state.phases.slice(1, 3)).toEqual(
      state.phases.filter((_, index) => index === 1 || index === 2),
    );
    expect(state.phases.at(-1)?.kind).toBe('profiler');
    expect(state.phases.concat([])).toEqual(state.phases);
    expect(Object.keys(state.phases)).toEqual(['0', '1', '2', '3']);
    expect(Object.getOwnPropertyDescriptor(state.phases, 'length')?.value).toBe(4);
    expect(JSON.parse(JSON.stringify(state.phases))).toEqual(state.phases);
  });

  it('keeps older public snapshots unchanged across indexed replacement and branching', () => {
    const started = applyRunMapEvent(
      applyRunMapEvent(initialRunMap(), runStarted('agent')),
      execution(2, 'agent_execution_started', 'a'),
    );
    const finished = applyRunMapEvent(started, execution(3, 'agent_execution_finished', 'a'));
    const branched = applyRunMapEvent(started, execution(4, 'agent_execution_started', 'b'));

    expect(started.phases[1]).toMatchObject({executionId: 'a', status: 'active'});
    expect(finished.phases[1]).toMatchObject({executionId: 'a', status: 'completed'});
    expect(branched.phases.filter(phase => phase.kind === 'implementer')).toMatchObject([
      {executionId: 'a', status: 'active'},
      {executionId: 'b', status: 'active'},
    ]);
    expect(started.rounds[0]?.activeAgentStarts).toEqual({
      'implementer:a': '2026-01-01T00:00:02Z',
    });
  });

  it('reuses public arrays when an event changes no round or phase fact', () => {
    const started = applyRunMapEvent(
      applyRunMapEvent(initialRunMap(), runStarted('agent')),
      execution(2, 'agent_execution_started', 'a'),
    );
    const output = applyRunMapEvent(started, execution(3, 'agent_output_chunk', 'a'));

    expect(output.rounds).toBe(started.rounds);
    expect(output.phases).toBe(started.phases);
    expect(output.lastEventTimestamp).toBe('2026-01-01T00:00:03Z');
  });

  it('preserves legacy finish precedence across concurrent same-role executions', () => {
    let state = applyRunMapEvent(emptyRunMap(), execution(1, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'b'));
    state = applyRunMapEvent(state, {
      sequence: 3,
      timestamp: '2026-01-01T00:00:03Z',
      type: 'phase_finished',
      status: 'completed',
      agent_kind: 'implementer',
      round_label: 'round-1-implementer',
      data: {kind: 'phase', phase: 'implementer', attempt: null},
    });

    expect(state.phases.filter(phase => phase.kind === 'implementer')).toMatchObject([
      {executionId: 'a', status: 'completed'},
      {executionId: 'b', status: 'active'},
    ]);
  });

  it('keeps native array length and indexed endpoints across tree depths', () => {
    let state = emptyRunMap();
    for (let round = 1; round <= 1_100; round += 1) {
      state = applyRunMapEvent(
        state,
        execution(round, 'agent_execution_started', `exec-${round}`, round),
      );
    }

    expect(state.rounds).toHaveLength(1_100);
    expect(state.rounds[0]?.number).toBe(1);
    expect(state.rounds.at(-1)?.number).toBe(1_100);
    expect(state.phases).toHaveLength(4_400);
    expect(state.phases[0]?.kind).toBe('orchestrator');
    expect(state.phases.at(-1)).toMatchObject({
      kind: 'profiler',
      roundNumber: 1_100,
      status: 'pending',
    });
  });
});

describe('run-level closeout', () => {
  it('closes every round and phase on interrupt, not just the labelled one', () => {
    let state = applyRunMapEvent(initialRunMap(), runStarted('agent'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, execution(3, 'agent_execution_finished', 'a'));
    state = applyRunMapEvent(state, roundFinished(4, 1));
    state = applyRunMapEvent(state, execution(5, 'agent_execution_started', 'b', 2));
    state = applyRunMapEvent(state, runInterrupted(6));

    expect(state.phases.map(phase => [phase.roundNumber, phase.kind, phase.status])).toEqual([
      [1, 'orchestrator', 'cancelled'],
      [1, 'implementer', 'completed'],
      [1, 'judge', 'cancelled'],
      [1, 'profiler', 'cancelled'],
      [2, 'orchestrator', 'cancelled'],
      [2, 'implementer', 'interrupted'],
      [2, 'judge', 'cancelled'],
      [2, 'profiler', 'cancelled'],
    ]);
    // Round one closed on its own event and keeps that status; round two was
    // still open, so the run-level event is what ends it.
    expect(state.rounds.map(round => [round.number, round.status])).toEqual([
      [1, 'completed'],
      [2, 'failed'],
    ]);
    expect(state.rounds.every(round => !hasActiveAgentTiming(round))).toBe(true);
  });

  it('fails the running agents and cancels the unstarted ones when a run fails', () => {
    let state = applyRunMapEvent(initialRunMap(), runStarted('agent'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, runFailed(3));

    expect(state.phases.map(phase => [phase.kind, phase.status])).toEqual([
      ['orchestrator', 'cancelled'],
      ['implementer', 'failed'],
      ['judge', 'cancelled'],
      ['profiler', 'cancelled'],
    ]);
    expect(requiredRound(state).status).toBe('failed');
  });

  it('never overwrites a phase that already reached a terminal status', () => {
    // The graceful teardown emits per-execution `phase_finished(interrupted)`
    // before the run-scoped event, so the sweep runs over phases it has already
    // settled and must leave both their status and their end alone.
    let state = applyRunMapEvent(emptyRunMap(), execution(1, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, {
      ...execution(2, 'phase_finished', 'a'),
      status: 'interrupted',
    });
    state = applyRunMapEvent(state, execution(3, 'agent_execution_started', 'b'));
    state = applyRunMapEvent(state, execution(4, 'agent_execution_finished', 'b'));
    state = applyRunMapEvent(state, runInterrupted(5));

    expect(state.phases.map(phase => [phase.kind, phase.status, phase.finishedAt])).toEqual([
      // Never started, so it is not run rather than ended, and has no end.
      ['orchestrator', 'cancelled', undefined],
      ['implementer', 'interrupted', '2026-01-01T00:00:02Z'],
      ['judge', 'cancelled', undefined],
      ['profiler', 'cancelled', undefined],
      ['implementer', 'completed', '2026-01-01T00:00:04Z'],
    ]);
  });

  it('closes the state a killed run left open when a resumed run starts', () => {
    // Repro step six: SIGKILL, so no terminal event is ever written. The next
    // thing the journal carries is the resumed process's `run_started`.
    let state = applyRunMapEvent(initialRunMap(), runStarted('agent'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));
    state = applyRunMapEvent(state, streamedChunk(5));
    state = applyRunMapEvent(state, {
      ...runStarted('agent'),
      sequence: 6,
      timestamp: '2026-01-01T01:00:00Z',
    });

    expect(state.phases.map(phase => [phase.kind, phase.status])).toEqual([
      ['orchestrator', 'cancelled'],
      ['implementer', 'interrupted'],
      ['judge', 'cancelled'],
      ['profiler', 'cancelled'],
    ]);
    const round = requiredRound(state);
    expect(round.status).toBe('failed');
    expect(hasActiveAgentTiming(round)).toBe(false);
    // Closed at the last event the dead process wrote (00:00:05), not at the
    // resume an hour later: the downtime is not the round's elapsed time.
    expect(roundAgentElapsedMs(round, new Date('2026-01-01T02:00:00Z'))).toBe(3000);
  });

  it('leaves a first run_started alone, having nothing open to close', () => {
    const state = applyRunMapEvent(initialRunMap(), runStarted('agent'));

    expect(state.rounds).toEqual([]);
    expect(state.phases).toEqual([]);
    expect(state.outerLoop).toBe('agent');
  });
});

describe('expected phase seeding', () => {
  it('seeds pending placeholders for every advertised role, even ones the legacy table never knew', () => {
    let state = applyRunMapEvent(
      initialRunMap(),
      runStarted('swarm', ['scout', 'implementer', 'reviewer']),
    );
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));

    expect(state.phases.map(phase => [phase.kind, phase.status])).toEqual([
      ['scout', 'pending'],
      ['implementer', 'active'],
      ['reviewer', 'pending'],
    ]);
  });

  it('lets a known loop advertise a role the legacy table lacks without a client edit', () => {
    let state = applyRunMapEvent(
      initialRunMap(),
      runStarted('agent', ['orchestrator', 'implementer', 'judge', 'profiler', 'benchmark']),
    );
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));

    expect(state.phases.map(phase => phase.kind)).toEqual([
      'orchestrator',
      'implementer',
      'judge',
      'profiler',
      'benchmark',
    ]);
  });

  it('falls back to the legacy table for a run_started without advertised roles', () => {
    let state = applyRunMapEvent(initialRunMap(), runStarted('plain'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));

    expect(state.phases.map(phase => phase.kind)).toEqual(['implementer', 'judge', 'perf_eval']);
  });

  it('treats an empty advertised list as absent and uses the fallback', () => {
    let state = applyRunMapEvent(initialRunMap(), runStarted('plain', []));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));

    expect(state.phases.map(phase => phase.kind)).toEqual(['implementer', 'judge', 'perf_eval']);
  });

  it('still tracks observed phases for an unknown loop with no advertised roles', () => {
    let state = applyRunMapEvent(initialRunMap(), runStarted('mystery'));
    state = applyRunMapEvent(state, execution(2, 'agent_execution_started', 'a'));

    // The graceful-degradation contract: no role set is known, so nothing is
    // seeded, and consumers can observe the condition through the selector.
    expect(expectedRolesForSeeding(state)).toBeNull();
    expect(state.phases.map(phase => [phase.kind, phase.status])).toEqual([
      ['implementer', 'active'],
    ]);
  });

  it('folds the advertised roles onto the state for consumers and prefix merges', () => {
    const state = applyRunMapEvent(initialRunMap(), runStarted('plain', ['implementer', 'judge']));

    expect(state.expectedRoles).toEqual(['implementer', 'judge']);
    expect(expectedRolesForSeeding(state)).toEqual(['implementer', 'judge']);
  });
});

function initialRunMap(): RunMapState {
  return {outerLoop: null, expectedRoles: null, rounds: [], phases: [], lastEventTimestamp: null};
}

/** A timestamped event with no agent scope, the shape run-scoped events have. */
function runScoped(sequence: number, type: RunEvent['type']): RunEvent {
  return {sequence, timestamp: `2026-01-01T00:00:0${sequence}Z`, type};
}

function runInterrupted(sequence: number): RunEvent {
  return {
    ...runScoped(sequence, 'run_interrupted'),
    data: {kind: 'run_interrupted', reason: 'operator', signal: 'SIGTERM'},
  };
}

function runFailed(sequence: number): RunEvent {
  return runScoped(sequence, 'run_failed');
}

/** An output chunk: folded for its timestamp alone, which no phase records. */
function streamedChunk(sequence: number): RunEvent {
  return runScoped(sequence, 'agent_output_chunk');
}

function roundFinished(sequence: number, roundNumber: number): RunEvent {
  return {
    ...runScoped(sequence, 'round_finished'),
    status: 'completed',
    round_label: `round-${roundNumber}`,
  };
}

function runStarted(outerLoop: string, expectedRoles?: string[]): RunEvent {
  return {
    sequence: 1,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'run_started',
    status: 'active',
    data: {
      kind: 'run_started',
      outer_loop: outerLoop,
      input: '/target',
      max_rounds: 3,
      ...(expectedRoles === undefined ? {} : {expected_roles: expectedRoles}),
    },
  };
}

function emptyRunMap(): RunMapState {
  return {
    outerLoop: 'agent',
    expectedRoles: null,
    rounds: [],
    phases: [],
    lastEventTimestamp: null,
  };
}

function requiredRound(state: RunMapState): RunMapState['rounds'][number] {
  const round = state.rounds[0];
  if (round === undefined) throw new Error('Expected one projected round');
  return round;
}

function execution(
  sequence: number,
  type: RunEvent['type'],
  executionId: string,
  roundNumber = 1,
): RunEvent {
  const started = type === 'agent_execution_started';
  return {
    sequence,
    timestamp: `2026-01-01T00:00:0${sequence}Z`,
    type,
    execution_id: executionId,
    invocation_id: executionId,
    agent_kind: 'implementer',
    round_label: `round-${roundNumber}-implementer`,
    ...(started
      ? {
          data: {
            kind: 'agent_execution_started',
            stage: 'implementation',
            attempt: 1,
            system_prompt: '',
            user_prompt: 'Implement',
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: 'Starting',
              tool: null,
            },
          },
        }
      : type === 'agent_execution_finished'
        ? {data: {kind: 'agent_execution_finished', error: null}}
        : {}),
  };
}
