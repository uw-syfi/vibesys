import {expect, test} from 'bun:test';
import {runPageProblems} from './index.js';
import {type RunPageState, runPageInitial, runPageStep} from './run-page.js';

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

test('a Resume that did not take is one error, and one nobody confirmed is one warning', () => {
  const resumed = (status: RunPageState['resumeStatus']) =>
    runPageProblems({...initial(), resumeStatus: status});
  expect(resumed(null)).toEqual([]);
  expect(resumed({pending: true, reason: 'Waiting for the run to start'})).toEqual([]);
  expect(
    resumed({pending: false, tone: 'err', title: 'Could not resume the run', detail: 'Host down'}),
  ).toEqual([
    {id: 'run/resume', tone: 'err', title: 'Could not resume the run', detail: 'Host down'},
  ]);
  expect(
    resumed({
      pending: false,
      tone: 'warn',
      title: 'Resume not confirmed',
      detail: 'Check the host before trying again.',
    }),
  ).toEqual([
    {
      id: 'run/resume',
      tone: 'warn',
      title: 'Resume not confirmed',
      detail: 'Check the host before trying again.',
    },
  ]);
});
