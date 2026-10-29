import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {
  IntentAction,
  IntentStateSchema,
  IntentStep,
  PostIntentRequestSchema,
  PostIntentResponseSchema,
  TerminateRunOutcome,
} from '@vibesys/proto';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {runIntentsMachine} from './index.js';

const request = create(PostIntentRequestSchema, {
  requestId: 'r-1',
  runId: 'run-1',
  projectRoot: '/project',
  expectedEpoch: 1n,
  intent: {case: 'pause', value: {}},
});

test('off starts neither reads nor writes, even when asked to send', () => {
  const off = getNextSnapshot(
    runIntentsMachine,
    getInitialSnapshot(runIntentsMachine, {enabled: false}),
    {type: 'send', request},
  );
  expect(off.value).toBe('disabled');
  expect(off.children).toEqual({});
  expect(off.context.entries).toEqual([]);
});

test('outcome before admission is never downgraded to pending', () => {
  const initial = getInitialSnapshot(runIntentsMachine, {enabled: true});
  const sending = getNextSnapshot(runIntentsMachine, initial, {type: 'send', request});
  const applied = getNextSnapshot(runIntentsMachine, sending, {
    type: 'outcomes',
    outcomes: [
      create(IntentStateSchema, {
        controlId: 'r-1',
        action: IntentAction.PAUSE,
        step: IntentStep.APPLIED,
      }),
    ],
  });
  const admitted = getNextSnapshot(runIntentsMachine, applied, {
    type: 'xstate.done.actor.post',
    output: create(PostIntentResponseSchema, {requestId: 'r-1'}),
  } as never);
  expect(admitted.context.entries[0]?.status).toBe('applied');
});

test('deadline retains the original request for an idempotent retry', () => {
  const sending = getNextSnapshot(
    runIntentsMachine,
    getInitialSnapshot(runIntentsMachine, {enabled: true}),
    {type: 'send', request},
  );
  const timedOut = getNextSnapshot(runIntentsMachine, sending, {
    type: 'xstate.after.deadline.runIntents.active.sending',
  } as never);
  expect(timedOut.context.entries[0]?.status).toBe('uncertain');
  const retry = getNextSnapshot(runIntentsMachine, timedOut, {type: 'retry', requestId: 'r-1'});
  expect(retry.context.current).toEqual(request);
  expect(retry.context.entries).toHaveLength(1);
});

test('model: requests are unique and operations and deadlines are owned', () => {
  checkMachine({
    machine: runIntentsMachine,
    input: {enabled: true},
    event: (random, live) =>
      random.pick([
        {type: 'send', request},
        {type: 'retry', requestId: 'r-1'},
        {type: 'watchFailed', error: 'disconnected'},
        {type: 'outcomes', outcomes: []},
        ...live.operations
          .filter(operation => operation === 'post')
          .map(operation =>
            live.done(operation, create(PostIntentResponseSchema, {requestId: 'r-1'})),
          ),
        ...live.operations.map(operation => live.error(operation, new Error('failed'))),
        ...live.delays.map(delay => live.fire(delay)),
      ]),
    invariants: snapshot =>
      new Set(snapshot.context.entries.map(entry => entry.request.requestId)).size ===
      snapshot.context.entries.length
        ? []
        : ['duplicate request'],
  });
});

test('an outcome stream failure does not change an in-flight admission', () => {
  const sending = getNextSnapshot(
    runIntentsMachine,
    getInitialSnapshot(runIntentsMachine, {enabled: true}),
    {type: 'send', request},
  );
  const disconnected = getNextSnapshot(runIntentsMachine, sending, {
    type: 'xstate.error.actor.watch',
    error: new Error('stream failed'),
  } as never);
  expect(disconnected.context.entries[0]?.status).toBe('sending');
  const admitted = getNextSnapshot(runIntentsMachine, disconnected, {
    type: 'xstate.done.actor.post',
    output: create(PostIntentResponseSchema, {requestId: 'r-1'}),
  } as never);
  expect(admitted.context.entries[0]?.status).toBe('pending');
});

test('retry owns the original payload even if its caller edits the generated message', () => {
  const mutable = create(PostIntentRequestSchema, {
    requestId: 'owned',
    runId: 'run-1',
    projectRoot: '/project',
    expectedEpoch: 1n,
    intent: {case: 'steer', value: {text: 'original guidance'}},
  });
  const sending = getNextSnapshot(
    runIntentsMachine,
    getInitialSnapshot(runIntentsMachine, {enabled: true}),
    {type: 'send', request: mutable},
  );
  mutable.requestId = 'changed';
  if (mutable.intent.case === 'steer') mutable.intent.value.text = 'changed guidance';
  const timedOut = getNextSnapshot(runIntentsMachine, sending, {
    type: 'xstate.after.deadline.runIntents.active.sending',
  } as never);
  const retry = getNextSnapshot(runIntentsMachine, timedOut, {type: 'retry', requestId: 'owned'});
  expect(retry.context.current?.requestId).toBe('owned');
  expect(retry.context.current?.intent).toMatchObject({
    case: 'steer',
    value: {text: 'original guidance'},
  });
});

test('an undeclared ordered reader is visible without claiming application', () => {
  const sending = getNextSnapshot(
    runIntentsMachine,
    getInitialSnapshot(runIntentsMachine, {enabled: true}),
    {type: 'send', request},
  );
  const admitted = getNextSnapshot(runIntentsMachine, sending, {
    type: 'xstate.done.actor.post',
    output: create(PostIntentResponseSchema, {requestId: 'r-1', orderedReader: false}),
  } as never);
  expect(admitted.context.entries[0]?.status).toBe('pending');
  expect(admitted.context.warning).toContain('Posting order has not been confirmed');
});

test('process observations stay pending until a canonical control outcome arrives', () => {
  for (const outcome of [
    TerminateRunOutcome.WRITER_RELEASED,
    TerminateRunOutcome.IDENTITY_CHANGED,
    TerminateRunOutcome.UNSUPPORTED,
    TerminateRunOutcome.REQUESTED_UNCONFIRMED,
    TerminateRunOutcome.OBSERVATION_UNKNOWN,
    TerminateRunOutcome.PROCESS_ABSENT,
  ]) {
    const sending = getNextSnapshot(
      runIntentsMachine,
      getInitialSnapshot(runIntentsMachine, {enabled: true}),
      {type: 'send', request},
    );
    const output = create(PostIntentResponseSchema, {
      requestId: 'r-1',
      orderedReader: true,
      termination: {requestId: 'r-1', outcome},
    });
    const admitted = getNextSnapshot(runIntentsMachine, sending, {
      type: 'xstate.done.actor.post',
      output,
    } as never);
    expect(admitted.context.entries[0]?.status).toBe('pending');
    expect(admitted.context.warning).not.toBeNull();
    const applied = getNextSnapshot(runIntentsMachine, admitted, {
      type: 'outcomes',
      outcomes: [
        create(IntentStateSchema, {
          controlId: 'r-1',
          action: IntentAction.PAUSE_NOW,
          step: IntentStep.APPLIED,
        }),
      ],
    });
    expect(applied.context.entries[0]?.status).toBe('applied');
    expect(applied.context.warning).toBeNull();
    const earlier = getNextSnapshot(runIntentsMachine, sending, {
      type: 'outcomes',
      outcomes: [
        create(IntentStateSchema, {
          controlId: 'r-1',
          action: IntentAction.PAUSE_NOW,
          step: IntentStep.APPLIED,
        }),
      ],
    });
    const late = getNextSnapshot(runIntentsMachine, earlier, {
      type: 'xstate.done.actor.post',
      output,
    } as never);
    expect(late.context.warning).toBeNull();
  }
});

test('replaying writer outcomes includes Resume and requests posted in another window exactly once', () => {
  let state = getInitialSnapshot(runIntentsMachine, {enabled: true});
  const outcomes = [
    create(IntentStateSchema, {
      controlId: 'elsewhere-pause',
      action: IntentAction.PAUSE,
      step: IntentStep.APPLIED,
    }),
    create(IntentStateSchema, {
      controlId: 'elsewhere-resume',
      action: IntentAction.RESUME,
      step: IntentStep.APPLIED,
    }),
  ];
  for (const batch of [outcomes.slice(0, 1), outcomes, outcomes.slice(1)]) {
    state = getNextSnapshot(runIntentsMachine, state, {type: 'outcomes', outcomes: batch});
  }
  expect(state.context.outcomes.map(row => [row.id, row.action, row.status])).toEqual([
    ['elsewhere-pause', IntentAction.PAUSE, 'applied'],
    ['elsewhere-resume', IntentAction.RESUME, 'applied'],
  ]);
  const resume = outcomes[1];
  if (!resume) throw new Error('missing resume outcome');
  resume.controlId = 'mutated';
  expect(state.context.outcomes[1]?.id).toBe('elsewhere-resume');
});
