import {expect, test} from 'bun:test';
import {registryReads, registryRunCatalog} from '@vibesys/connection';
import {FakeReadCatalog} from '@vibesys/connection/testing';
import type {RunInputs} from '@vibesys/ipc';
import {NewRunMain} from './index.js';

const HOST = {kind: 'local'} as const;
const INPUTS: RunInputs = {
  project: '/project',
  task: 'queue',
  harness: null,
  options: {},
  agents: {},
  measure_on: {kind: 'same_machine'},
  args: [],
};

function subject(peer: FakeReadCatalog) {
  return new NewRunMain({
    remember: async () => {},
    now: () => 1,
    registryCatalog: registryRunCatalog(peer),
    registryReads: registryReads(peer),
  });
}

test('regression: v2 preflight, examples and task goals never acquire a helper connection', async () => {
  const peer = new FakeReadCatalog();
  peer.goals.set('/project/queue', {task: 'queue', goal: 'Improve throughput', objectives: []});
  peer.exampleList = {
    version: 1n,
    checkout: '/checkout',
    examples: [{name: 'queue', category: 'systems', tasks: ['queue']}],
  };
  const main = subject(peer);
  expect(await main.answer({kind: 'describe-machine', host: HOST})).toMatchObject({
    kind: 'offered',
    value: {hardware: {cpu_cores: 8, accelerator: 'cpu'}},
  });
  expect(await main.answer({kind: 'preflight', host: HOST, inputs: INPUTS})).toEqual({
    kind: 'offered',
    value: {checks: [], roles: null, backend: null},
  });
  expect(await main.answer({kind: 'examples', host: HOST})).toMatchObject({
    kind: 'offered',
    value: {examples: [{name: 'queue'}]},
  });
  expect(
    await main.answer({kind: 'task-goal', host: HOST, project: '/project', task: 'queue'}),
  ).toMatchObject({kind: 'offered', value: {task: 'queue', goal: 'Improve throughput'}});
  expect(peer.requests.map(request => request.method.case)).toEqual([
    'describeMachine',
    'preflightRun',
    'listExamples',
    'getTaskGoal',
  ]);
});

test('v2 read failures propagate without helper fallback', async () => {
  const peer = new FakeReadCatalog();
  peer.failure = new Error('gateway denied');
  const main = subject(peer);
  for (const request of [
    {kind: 'preflight', host: HOST, inputs: INPUTS},
    {kind: 'examples', host: HOST},
    {kind: 'task-goal', host: HOST, project: '/project', task: 'queue'},
    {kind: 'describe-machine', host: HOST},
  ] as const) {
    await expect(main.answer(request)).rejects.toThrow('gateway denied');
  }
});

test('a missing catalog fails instead of answering', async () => {
  const main = new NewRunMain({
    remember: async () => {},
    now: () => 1,
  });
  await expect(main.answer({kind: 'examples', host: HOST})).rejects.toThrow(
    'new-run registry is missing',
  );
});
