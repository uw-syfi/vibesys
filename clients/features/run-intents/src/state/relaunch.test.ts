import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {RelaunchResponseSchema} from '@vibesys/proto';
import {relaunchRequest} from '@vibesys/runs/intents';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {relaunchMachine} from './index.js';

test('relaunch owns its request and cannot repeat an uncertain launch', () => {
  const request = relaunchRequest('/project', 'run-1', 1n);
  const initial = getInitialSnapshot(relaunchMachine, {request});
  request.runId = 'other';
  const launching = getNextSnapshot(relaunchMachine, initial, {type: 'relaunch'});
  expect(launching.context.request.runId).toBe('run-1');
  const timedOut = getNextSnapshot(relaunchMachine, launching, {
    type: 'xstate.after.deadline.relaunch.launching',
  } as never);
  const retry = getNextSnapshot(relaunchMachine, timedOut, {type: 'relaunch'});
  expect(retry.value).toBe('uncertain');
  expect(retry.children).toEqual({});
});

test('model: launch and its deadline always belong to the launching state', () => {
  checkMachine({
    machine: relaunchMachine,
    input: {request: relaunchRequest('/project', 'run-1', 1n)},
    event: (random, live) =>
      random.pick([
        {type: 'relaunch'},
        ...live.operations.map(operation =>
          live.done(
            operation,
            create(RelaunchResponseSchema, {outcome: {case: 'epoch', value: 2n}}),
          ),
        ),
        ...live.operations.map(operation => live.error(operation, new Error('disconnected'))),
        ...live.delays.map(delay => live.fire(delay)),
      ]),
    invariants: snapshot =>
      snapshot.context.result !== null && !snapshot.matches('settled')
        ? ['result outside settled state']
        : [],
  });
});
