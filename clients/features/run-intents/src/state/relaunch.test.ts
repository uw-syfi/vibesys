import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {LifecycleSchema, RelaunchResponseSchema, RunLiveness} from '@vibesys/proto';
import {relaunchRequest} from '@vibesys/runs/intents';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {relaunchMachine} from './index.js';

test('relaunch owns its request and cannot repeat an uncertain launch', () => {
  const request = relaunchRequest('/project', 'run-1', 1n);
  const initial = getInitialSnapshot(relaunchMachine, {
    request,
    observation: {
      epoch: 1n,
      lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
    },
  });
  request.runId = 'other';
  const launching = getNextSnapshot(relaunchMachine, initial, {type: 'relaunch'});
  expect(launching.context.request.runId).toBe('run-1');
  const timedOut = getNextSnapshot(relaunchMachine, launching, {
    type: 'xstate.after.deadline.relaunch.launching',
  } as never);
  const retry = getNextSnapshot(relaunchMachine, timedOut, {type: 'relaunch'});
  expect(retry.value).toBe('uncertain');
  expect(Object.keys(retry.children)).toEqual(['liveness']);
});

test('model: launch and its deadline always belong to the launching state', () => {
  checkMachine({
    machine: relaunchMachine,
    input: {
      request: relaunchRequest('/project', 'run-1', 1n),
      observation: {
        epoch: 1n,
        lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
      },
    },
    event: (random, live) =>
      random.pick([
        {type: 'relaunch'},
        {
          type: 'observe',
          observation: {
            epoch: random.pick([1n, 2n]),
            lifecycle: create(LifecycleSchema, {
              state: {case: random.pick(['parked', 'running'] as const), value: {}},
            }),
          },
        },
        {
          type: 'liveness',
          value: {
            epoch: random.pick([1n, 2n]),
            value: random.pick([RunLiveness.ALIVE, RunLiveness.INTERRUPTED, RunLiveness.ENDED]),
          },
        },

        ...live.operations
          .filter(operation => operation === 'launch')
          .map(operation =>
            live.done(
              operation,
              create(RelaunchResponseSchema, {outcome: {case: 'epoch', value: 2n}}),
            ),
          ),
        ...live.operations.map(operation => live.error(operation, new Error('disconnected'))),
        ...live.delays.map(delay => live.fire(delay)),
      ]),
    invariants: snapshot =>
      snapshot.context.result !== null && snapshot.matches('launching')
        ? ['prior result retained during a new launch']
        : [],
  });
});

test('observations are owned and an older epoch cannot regress the launch target', () => {
  const lifecycle = create(LifecycleSchema, {state: {case: 'parked', value: {}}});
  const initial = getInitialSnapshot(relaunchMachine, {
    request: relaunchRequest('/project', 'run-1', 2n),
    observation: {epoch: 2n, lifecycle},
  });
  lifecycle.state = {case: undefined};
  const stale = getNextSnapshot(relaunchMachine, initial, {
    type: 'observe',
    observation: {epoch: 1n},
  });
  expect(stale.context.observation.epoch).toBe(2n);
  expect(getNextSnapshot(relaunchMachine, stale, {type: 'relaunch'}).value).toBe('launching');
});
