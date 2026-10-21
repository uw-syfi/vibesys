import {expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {gatewayRecordsMachine} from './index.js';

test('default read never starts Tail when the per-host setting is off', () => {
  const initial = getInitialSnapshot(gatewayRecordsMachine);
  const off = getNextSnapshot(gatewayRecordsMachine, initial, {
    type: 'xstate.done.actor.read',
    output: false,
  } as never);
  expect(off.value).toBe('off');
  expect(off.context.enabled).toBe(false);
  expect(off.children).toEqual({});
});
test('model: operations and deadlines remain owned by the current setting and stream state', () => {
  checkMachine({
    machine: gatewayRecordsMachine,
    input: undefined,
    event: (random, live) =>
      random.pick([
        {type: 'toggle', enabled: random.pick([true, false])},
        {type: 'records', records: []},
        {type: 'failed', error: 'disconnected'},
        ...live.operations.map(operation => live.done(operation, random.pick([true, false]))),
        ...live.operations.map(operation => live.error(operation, new Error('failed'))),
        ...live.delays.map(delay => live.fire(delay)),
      ]),
    invariants: snapshot =>
      snapshot.context.rows.length <= 1000 ? [] : ['record retention exceeded'],
  });
});
