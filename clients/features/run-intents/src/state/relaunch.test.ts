import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {LifecycleSchema, RelaunchResponseSchema, RunLiveness} from '@vibesys/proto';
import {relaunchRequest} from '@vibesys/runs/intents';
import fc from 'fast-check';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {relaunchMachine} from './index.js';

test('property: equivalent writer observations are no-ops for the observation and the writer fact is taken as given', () => {
  fc.assert(
    fc.property(fc.integer({min: 1, max: 100}), fc.boolean(), (epoch, parked) => {
      const lifecycle = create(LifecycleSchema, {
        state: {case: parked ? 'parked' : 'running', value: {}},
      });
      const input = {
        request: relaunchRequest('/project', 'run-1', BigInt(epoch)),
        observation: {epoch: BigInt(epoch), lifecycle},
      };
      const initial = getInitialSnapshot(relaunchMachine, input);
      const repeated = getNextSnapshot(relaunchMachine, initial, {
        type: 'writer',
        facts: {
          observation: input.observation,
          liveness: {epoch: BigInt(epoch), value: RunLiveness.ALIVE},
          fresh: true,
        },
      });
      expect(repeated.context.observation).toBe(initial.context.observation);
      expect(repeated.context.liveness).toEqual({epoch: BigInt(epoch), value: RunLiveness.ALIVE});
      const older = getNextSnapshot(relaunchMachine, repeated, {
        type: 'writer',
        facts: {
          observation: {epoch: BigInt(epoch - 1)},
          liveness: {epoch: 0n, value: RunLiveness.UNSPECIFIED},
          fresh: false,
        },
      });
      expect(older.context.observation).toBe(initial.context.observation);
      expect(older.context.liveness).toEqual({epoch: 0n, value: RunLiveness.UNSPECIFIED});
    }),
    {numRuns: 80, seed: 253},
  );
});

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
  expect(Object.keys(retry.children)).toEqual([]);
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
          type: 'writer',
          facts: {
            observation: {
              epoch: random.pick([1n, 2n]),
              lifecycle: create(LifecycleSchema, {
                state: {case: random.pick(['parked', 'running'] as const), value: {}},
              }),
            },
            liveness: {
              epoch: random.pick([1n, 2n]),
              value: random.pick([RunLiveness.ALIVE, RunLiveness.INTERRUPTED, RunLiveness.ENDED]),
            },
            fresh: random.pick([true, false]),
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
    type: 'writer',
    facts: {
      observation: {epoch: 1n},
      liveness: {epoch: 0n, value: RunLiveness.UNSPECIFIED},
      fresh: true,
    },
  });
  expect(stale.context.observation.epoch).toBe(2n);
  expect(getNextSnapshot(relaunchMachine, stale, {type: 'relaunch'}).value).toBe('launching');
});
