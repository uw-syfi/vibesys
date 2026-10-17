import {expect, test} from 'bun:test';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import {type RunPageState, runPageInitial} from './run-page.js';
import {shownStream} from './views.js';

function stream(id: string, owner: LogStream['owner']): LogStream {
  return {id, owner, command: 'cmd', retained_start: 0, size: 0, outcome: 'ended'};
}

const STREAMS: readonly LogStream[] = [
  stream('image-1', {kind: 'startup_step', stage: 'building_image'}),
  stream('container-1', {kind: 'startup_step', stage: 'starting_container'}),
  stream('narration-1', {kind: 'run_narration'}),
  stream('image-2', {kind: 'startup_step', stage: 'building_image'}),
  stream('narration-2', {kind: 'run_narration'}),
];

function logs(phase: HostRun['phase'], streams: readonly LogStream[], selected: string | null) {
  const run: HostRun = {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/queue-rs',
    runId: 'run',
    status: phase,
    phase,
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  };
  const state: RunPageState = {
    ...runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run'},
      title: {project: 'queue-rs', task: 'mpmc'},
      hostLabel: 'host-b',
      unavailable: {},
    }),
    record: phase === 'completed' ? {kind: 'ended', run} : {kind: 'live', run},
    logs: {kind: 'listed', instanceId: run.id, streams},
    selected,
  };
  return state;
}

test('the Logs tab opens on what the run says about itself, not on a docker build', () => {
  expect(shownStream(logs('running', STREAMS, null))).toBe('narration-2');
  expect(shownStream(logs('completed', STREAMS, null))).toBe('narration-2');
});

test('while the run starts, or without narration, the Logs tab opens on the first stream', () => {
  expect(shownStream(logs('starting', STREAMS, null))).toBe('image-1');
  expect(shownStream(logs('running', STREAMS.slice(0, 2), null))).toBe('image-1');
  expect(shownStream(logs('running', [], null))).toBeNull();
});

test('a stream the user chose stays shown while it is listed', () => {
  expect(shownStream(logs('running', STREAMS, 'image-2'))).toBe('image-2');
  expect(shownStream(logs('running', STREAMS, 'gone'))).toBe('narration-2');
});
