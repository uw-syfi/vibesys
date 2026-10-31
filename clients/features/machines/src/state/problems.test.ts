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

test('a machine action that failed is a problem worded as the action said, until the next action', () => {
  const listed: MachinesAction = {type: 'listed', token: 1, result: {ok: true, value: ENTRIES}};
  const asked = machinesStep(drive([listed]), {type: 'sign-in', host: 'ssh:host-a'});
  const failed = machinesStep(asked.state, {
    type: 'done',
    token: asked.state.pending?.token ?? 0,
    result: {ok: false, error: 'The sign-in did not finish.'},
  }).state;
  expect(machinesProblems(failed)).toEqual([
    {id: 'machines/action', tone: 'err', title: 'The sign-in did not finish.'},
  ]);
  const again = machinesStep(failed, {type: 'sign-in', host: 'ssh:host-a'}).state;
  expect(machinesProblems(again)).toEqual([]);
});

test('nothing wrong is no problem', () => {
  expect(machinesProblems(initialMachines().state)).toEqual([]);
});
