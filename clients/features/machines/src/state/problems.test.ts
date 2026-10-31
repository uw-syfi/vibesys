import {expect, test} from 'bun:test';
import {initialMachines, type MachinesAction, type MachinesState, machinesStep} from './index.js';
import {machinesProblems} from './problems.js';

const ENTRIES = [{host: 'ssh:host-a', label: 'host-a', checkout: '~/vibesys'}];

function drive(actions: readonly MachinesAction[]): MachinesState {
  let {state} = initialMachines();
  for (const action of actions) state = machinesStep(state, action).state;
  return state;
}

test('a listing that failed is one problem, until a listing succeeds', () => {
  const failed = drive([
    {type: 'listed', token: 1, result: {ok: false, error: 'hosts.json is unreadable.'}},
  ]);
  expect(machinesProblems(failed)).toEqual([
    {
      id: 'machines/list',
      tone: 'err',
      title: 'The machines could not be read',
      detail: 'hosts.json is unreadable.',
    },
  ]);
  expect(
    machinesProblems(drive([{type: 'listed', token: 1, result: {ok: true, value: ENTRIES}}])),
  ).toEqual([]);
});

const RAW = 'ssh: connect to host host-a port 22: Connection refused';

/** `state` after `kind` was asked of `host-a` and answered with `result`. */
function answered(
  state: MachinesState,
  kind: 'remove' | 'sign-in',
  result: Extract<MachinesAction, {type: 'done'}>['result'],
): MachinesState {
  const asked = machinesStep(state, {type: kind, host: 'ssh:host-a'}).state;
  return machinesStep(asked, {type: 'done', token: asked.pending?.token ?? 0, result}).state;
}

test('a machine action that failed is a problem titled by the action, with what the machine said as its detail', () => {
  const listed: MachinesAction = {type: 'listed', token: 1, result: {ok: true, value: ENTRIES}};
  const failed = answered(drive([listed]), 'sign-in', {ok: false, error: RAW});
  expect(machinesProblems(failed)).toEqual([
    {
      id: `machines/action/${failed.actionProblem?.token}`,
      tone: 'err',
      title: 'The sign-in did not finish.',
      detail: RAW,
    },
  ]);
  // Words that are already the title are not said twice.
  const worded = answered(drive([listed]), 'remove', {
    ok: false,
    error: 'The machine was not removed.',
  });
  expect(machinesProblems(worded).map(problem => problem.detail)).toEqual([undefined]);
});

test('a failed action stays until one succeeds, and one that fails again is a new problem', () => {
  const listed: MachinesAction = {type: 'listed', token: 1, result: {ok: true, value: ENTRIES}};
  const first = answered(drive([listed]), 'sign-in', {ok: false, error: RAW});
  // Asking again leaves it standing until the answer.
  const asked = machinesStep(first, {type: 'sign-in', host: 'ssh:host-a'}).state;
  expect(machinesProblems(asked)).toEqual(machinesProblems(first));
  // Failing again is a new problem, so a toast that was dismissed shows again.
  const second = answered(first, 'sign-in', {ok: false, error: RAW});
  expect(machinesProblems(second)[0]?.id).not.toBe(machinesProblems(first)[0]?.id);
  // An action that took ends it.
  expect(machinesProblems(answered(second, 'sign-in', {ok: true, value: null}))).toEqual([]);
});

test('nothing wrong is no problem', () => {
  expect(machinesProblems(initialMachines().state)).toEqual([]);
});
