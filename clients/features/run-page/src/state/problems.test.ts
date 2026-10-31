import {expect, test} from 'bun:test';
import {runPageProblems} from './index.js';
import {runPageInitial, runPageStep} from './run-page.js';

const initial = () =>
  runPageInitial({
    identity: {host: 'ssh:host-b', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });

test('a command the run did not take is one problem, until the next command', () => {
  expect(runPageProblems(initial())).toEqual([]);
  const refused = runPageStep(initial(), {
    type: 'command-refused',
    message: 'The run is not accepting commands.',
  });
  expect(runPageProblems(refused.state)).toEqual([
    {
      id: 'run/command',
      tone: 'err',
      title: 'The run did not take the command',
      detail: 'The run is not accepting commands.',
    },
  ]);
});
