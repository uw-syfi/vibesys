import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {
  IntentAction,
  IntentStateSchema,
  IntentStep,
  PostIntentRequestSchema,
  PostIntentResponseSchema,
} from '@vibesys/proto';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {runIntentsMachine} from './index.js';

const request = create(PostIntentRequestSchema, {
  requestId: 'r-1',
  runId: 'run-1',
  projectRoot: '/project',
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
