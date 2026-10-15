import {describe, expect, test} from 'bun:test';
import type {EventBatchMessage} from '@vibesys/contracts';
import {event, eventBatch, statusEvent} from '@vibesys/contracts/testing';
import {initialIterationNavigation} from '@vibesys/run-state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {createRunStore} from './index.js';

function navigation(runId: string | null, revision: string) {
  return {...initialIterationNavigation(), runId, revision};
}

function replaceRun(store: ReturnType<typeof createRunStore>, runId: string): void {
  store.dispatch({
    type: 'batch',
    message: eventBatch([event(1, 'server_ready', {run_id: runId})], {
      through_sequence: 1,
      history_after_sequence: 0,
      rebootstrap: true,
    }),
    reconciliation: {kind: 'rebootstrap', historyFloor: 0},
  });
}

test('distinguishes an omitted active-execution checkpoint from an empty one', () => {
  const store = createRunStore();

  store.dispatch({
    type: 'batch',
    message: eventBatch([
      event(1, 'agent_execution_started', {
        execution_id: 'exec-1',
        agent_kind: 'implementer',
        round_label: 'round-1-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement the change',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Starting',
            tool: null,
          },
        },
      }),
    ]),
    reconciliation: {kind: 'extend', historyFloor: 0},
  });

  expect(Object.keys(store.getState().activeExecutions)).toEqual(['exec-1']);

  store.dispatch({
    type: 'batch',
    message: eventBatch([], {active_executions: [], through_sequence: 1}),
    reconciliation: {kind: 'extend', historyFloor: 0},
  });

  expect(store.getState().activeExecutions).toEqual({});
});

test('keeps the last complete execution checkpoint across a fragmented rebootstrap', () => {
  const store = createRunStore();
  store.dispatch({
    type: 'batch',
    message: eventBatch([
      event(1, 'agent_execution_started', {
        execution_id: 'previous',
        agent_kind: 'implementer',
        round_label: 'round-1-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Previous assignment',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Working',
            tool: null,
          },
        },
      }),
      statusEvent(2, 'previous', 'agent_output_chunk', {progress: 'Still working'}),
    ]),
    reconciliation: {kind: 'extend', historyFloor: 0},
  });

  store.dispatch({
    type: 'batch',
    message: eventBatch([event(1, 'server_ready')], {
      active_executions: null,
      active_executions_checkpoint_id: 'a'.repeat(64),
      through_sequence: 1,
      rebootstrap: true,
    }),
    reconciliation: {kind: 'rebootstrap', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'deferred', checkpointId: 'a'.repeat(64)},
  });
  expect(Object.keys(store.getState().activeExecutions)).toEqual(['previous']);
  expect(store.getState().executionStatuses['previous']?.progress).toBe('Still working');

  store.dispatch({
    type: 'batch',
    message: eventBatch([], {
      active_executions: [
        {
          execution_id: 'replacement',
          agent_kind: 'judge',
          round_label: 'round-1-judge',
          stage: 'judge',
          attempt: null,
          assignment: 'Review',
          started_at: '2026-01-01T00:00:00Z',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Reviewing',
            tool: null,
          },
          provider: null,
          model: null,
        },
      ],
      through_sequence: 1,
    }),
    reconciliation: {kind: 'extend', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'complete', checkpointId: 'a'.repeat(64)},
  });
  expect(Object.keys(store.getState().activeExecutions)).toEqual(['replacement']);
  expect(store.getState().executionStatuses).toEqual({});
});

test('completes a fragmented rebootstrap with the ordinary rebootstrap state', () => {
  const createPreviousStore = () => {
    const store = createRunStore();
    store.dispatch({
      type: 'batch',
      message: eventBatch([
        event(8, 'agent_execution_started', {
          execution_id: 'shared',
          agent_kind: 'implementer',
          round_label: 'round-0-implementer',
          data: {
            kind: 'agent_execution_started',
            stage: 'implementation',
            attempt: 1,
            system_prompt: '',
            user_prompt: 'Previous assignment',
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: 'Working',
              tool: null,
            },
          },
        }),
        statusEvent(9, 'shared', 'agent_output_chunk', {progress: 'Newer previous progress'}),
      ]),
      reconciliation: {kind: 'extend', historyFloor: 0},
    });
    return store;
  };
  const ordinary = createPreviousStore();
  const fragmented = createPreviousStore();
  const replay = [
    event(1, 'server_ready'),
    event(2, 'agent_execution_started', {
      execution_id: 'shared',
      agent_kind: 'judge',
      round_label: 'round-1-judge',
      data: {
        kind: 'agent_execution_started',
        stage: 'judge',
        attempt: null,
        system_prompt: '',
        user_prompt: 'Review',
        activity: {
          kind: 'agent_execution_activity_changed',
          mode: 'thinking',
          summary: 'Reviewing',
          tool: null,
        },
      },
    }),
    statusEvent(
      3,
      'shared',
      'agent_output_chunk',
      {progress: 'Reviewing output'},
      {agent_kind: 'judge', round_label: 'round-1-judge'},
    ),
  ];
  const checkpoint = [
    {
      execution_id: 'shared',
      agent_kind: 'judge',
      round_label: 'round-1-judge',
      stage: 'judge',
      attempt: null,
      assignment: 'Review',
      started_at: '2026-01-01T00:00:02Z',
      activity: {
        kind: 'agent_execution_activity_changed',
        mode: 'thinking',
        summary: 'Reviewing',
        tool: null,
      },
      provider: null,
      model: null,
    },
  ] satisfies NonNullable<EventBatchMessage['active_executions']>;

  ordinary.dispatch({
    type: 'batch',
    message: eventBatch(replay, {
      active_executions: checkpoint,
      through_sequence: 3,
      rebootstrap: true,
    }),
    reconciliation: {kind: 'rebootstrap', historyFloor: 0},
  });
  fragmented.dispatch({
    type: 'batch',
    message: eventBatch(replay.slice(0, 2), {
      active_executions: null,
      active_executions_checkpoint_id: 'a'.repeat(64),
      through_sequence: 2,
      rebootstrap: true,
    }),
    reconciliation: {kind: 'rebootstrap', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'deferred', checkpointId: 'a'.repeat(64)},
  });
  expect(Object.keys(fragmented.getState().activeExecutions)).toEqual(['shared']);
  expect(fragmented.getState().executionStatuses['shared']?.progress).toBe(
    'Newer previous progress',
  );

  fragmented.dispatch({
    type: 'batch',
    message: eventBatch(replay.slice(2), {
      active_executions: null,
      active_executions_checkpoint_id: 'a'.repeat(64),
      through_sequence: 3,
      rebootstrap: false,
    }),
    reconciliation: {kind: 'extend', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'deferred', checkpointId: 'a'.repeat(64)},
  });
  expect(fragmented.getState().executionStatuses['shared']?.progress).toBe(
    'Newer previous progress',
  );

  fragmented.dispatch({
    type: 'batch',
    message: eventBatch([], {
      active_executions: checkpoint,
      through_sequence: 3,
      rebootstrap: false,
    }),
    reconciliation: {kind: 'extend', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'complete', checkpointId: 'a'.repeat(64)},
  });

  expect(fragmented.getState()).toEqual(ordinary.getState());
  expect(fragmented.getState().executionStatuses['shared']?.progress).toBe('Reviewing output');
});

test('carries staged execution events through checkpoint supersession', () => {
  const store = createRunStore();
  store.dispatch({
    type: 'batch',
    message: eventBatch([
      event(8, 'agent_execution_started', {
        execution_id: 'previous',
        agent_kind: 'implementer',
        round_label: 'round-0-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Previous assignment',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Working',
            tool: null,
          },
        },
      }),
      statusEvent(9, 'previous', 'agent_output_chunk', {progress: 'Previous progress'}),
    ]),
    reconciliation: {kind: 'extend', historyFloor: 0},
  });
  store.dispatch({
    type: 'batch',
    message: eventBatch(
      [
        event(1, 'server_ready'),
        event(2, 'agent_execution_started', {
          execution_id: 'replacement',
          agent_kind: 'judge',
          round_label: 'round-1-judge',
          data: {
            kind: 'agent_execution_started',
            stage: 'judge',
            attempt: null,
            system_prompt: '',
            user_prompt: 'Review',
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: 'Reviewing',
              tool: null,
            },
          },
        }),
      ],
      {
        active_executions: null,
        active_executions_checkpoint_id: 'a'.repeat(64),
        through_sequence: 2,
        rebootstrap: true,
      },
    ),
    reconciliation: {kind: 'rebootstrap', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'deferred', checkpointId: 'a'.repeat(64)},
  });
  store.dispatch({
    type: 'batch',
    message: eventBatch(
      [statusEvent(3, 'replacement', 'agent_output_chunk', {progress: 'Reviewing output'})],
      {
        active_executions: null,
        active_executions_checkpoint_id: 'b'.repeat(64),
        through_sequence: 3,
      },
    ),
    reconciliation: {kind: 'extend', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'deferred', checkpointId: 'b'.repeat(64)},
  });

  expect(Object.keys(store.getState().activeExecutions)).toEqual(['previous']);
  expect(store.getState().executionStatuses['previous']?.progress).toBe('Previous progress');

  store.dispatch({
    type: 'batch',
    message: eventBatch([], {
      active_executions: [
        {
          execution_id: 'replacement',
          agent_kind: 'judge',
          round_label: 'round-1-judge',
          stage: 'judge',
          attempt: null,
          assignment: 'Review',
          started_at: '2026-01-01T00:00:02Z',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Reviewing',
            tool: null,
          },
          provider: null,
          model: null,
        },
      ],
      through_sequence: 3,
    }),
    reconciliation: {kind: 'extend', historyFloor: 0},
  });

  expect(Object.keys(store.getState().activeExecutions)).toEqual(['replacement']);
  expect(store.getState().executionStatuses['replacement']?.progress).toBe('Reviewing output');
});

test('fragmented rebootstrap never carries status across sequence spaces', () => {
  const createPreviousStore = () => {
    const store = createRunStore();
    store.dispatch({
      type: 'batch',
      message: eventBatch([
        event(8, 'agent_execution_started', {
          execution_id: 'shared',
          agent_kind: 'implementer',
          round_label: 'round-0-implementer',
          data: {
            kind: 'agent_execution_started',
            stage: 'implementation',
            attempt: 1,
            system_prompt: '',
            user_prompt: 'Previous assignment',
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: 'Working',
              tool: null,
            },
          },
        }),
        statusEvent(9, 'shared', 'agent_output_chunk', {progress: 'STALE'}),
      ]),
      reconciliation: {kind: 'extend', historyFloor: 0},
    });
    return store;
  };
  const checkpoint = [
    {
      execution_id: 'shared',
      agent_kind: 'judge',
      round_label: 'round-1-judge',
      stage: 'judge',
      attempt: null,
      assignment: 'Review',
      started_at: '2026-01-01T00:00:02Z',
      activity: {
        kind: 'agent_execution_activity_changed',
        mode: 'thinking',
        summary: 'Reviewing',
        tool: null,
      },
      provider: null,
      model: null,
    },
  ] satisfies NonNullable<EventBatchMessage['active_executions']>;
  const replay = [event(1, 'server_ready')];
  const ordinary = createPreviousStore();
  const fragmented = createPreviousStore();

  ordinary.dispatch({
    type: 'batch',
    message: eventBatch(replay, {
      active_executions: checkpoint,
      through_sequence: 1,
      rebootstrap: true,
    }),
    reconciliation: {kind: 'rebootstrap', historyFloor: 0},
  });
  fragmented.dispatch({
    type: 'batch',
    message: eventBatch(replay, {
      active_executions: null,
      active_executions_checkpoint_id: 'a'.repeat(64),
      through_sequence: 1,
      rebootstrap: true,
    }),
    reconciliation: {kind: 'rebootstrap', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'deferred', checkpointId: 'a'.repeat(64)},
  });
  fragmented.dispatch({
    type: 'batch',
    message: eventBatch([], {
      active_executions: checkpoint,
      through_sequence: 1,
      rebootstrap: false,
    }),
    reconciliation: {kind: 'extend', historyFloor: 0},
    activeExecutionCheckpoint: {kind: 'complete', checkpointId: 'a'.repeat(64)},
  });

  expect(fragmented.getState()).toEqual(ordinary.getState());
  expect(fragmented.getState().executionStatuses).toEqual({});
});

describe('iteration navigation ownership', () => {
  test('rejects a foreign navigation result and preserves the last matching view', () => {
    const store = createRunStore();
    replaceRun(store, 'run-1');
    const matching = navigation('run-1', 'matching');

    expect(store.publishIterations(matching)).toEqual({kind: 'published'});
    expect(store.publishIterations(navigation('run-2', 'foreign'))).toEqual({
      kind: 'run-mismatch',
      expectedRunId: 'run-1',
      receivedRunId: 'run-2',
    });
    expect(store.getIterations()).toBe(matching);
  });

  test('defers navigation until run identity is known, then publishes only a match', () => {
    const matching = createRunStore();
    const waiting = navigation('run-1', 'waiting');
    expect(matching.publishIterations(waiting)).toEqual({kind: 'deferred'});
    expect(matching.getIterations().runId).toBeNull();

    replaceRun(matching, 'run-1');
    expect(matching.getIterations()).toBe(waiting);

    const foreign = createRunStore();
    expect(foreign.publishIterations(navigation('run-2', 'foreign'))).toEqual({kind: 'deferred'});
    expect(foreign.getIterations().runId).toBeNull();
    expect(
      foreign.dispatch({
        type: 'snapshot',
        snapshot: {run_id: 'run-1', sequence: 0, status: 'running'},
      }),
    ).toEqual({
      kind: 'run-mismatch',
      expectedRunId: 'run-1',
      receivedRunId: 'run-2',
    });
    expect(foreign.getIterations().runId).toBeNull();
  });

  test('keeps the last proven view while identity is unknown and clears it for a replacement run', () => {
    const store = createRunStore();
    replaceRun(store, 'run-1');
    const proven = navigation('run-1', 'proven');
    store.publishIterations(proven);

    store.dispatch({
      type: 'batch',
      message: eventBatch([event(1, 'server_ready')], {
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
      reconciliation: {kind: 'rebootstrap', historyFloor: 0},
    });
    expect(store.getState().runId).toBeNull();
    expect(store.getIterations()).toBe(proven);

    replaceRun(store, 'run-2');
    expect(store.getIterations()).toEqual(initialIterationNavigation());
  });

  test('never exposes navigation owned by another run across seeded action sequences', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const store = createRunStore();
      for (let step = 0; step < 60; step += 1) {
        if (random.bool()) {
          const runId = random.pick([null, 'run-1', 'run-2', 'run-3'] as const);
          store.publishIterations(navigation(runId, `revision-${seed}-${step}`));
        } else {
          replaceRun(store, random.pick(['run-1', 'run-2', 'run-3'] as const));
        }
        const runId = store.getState().runId;
        const navigationRunId = store.getIterations().runId;
        expect(runId === null || navigationRunId === null || navigationRunId === runId).toBe(true);
      }
    }
  });
});
