import {expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {runRecord} from './record.js';
import {runPageInitial, runPageStep} from './run-page.js';

const recent: RecentRun = {
  host: 'ssh:gpu',
  hostLabel: 'gpu',
  project: '/p',
  task: 'mpmc',
  runId: 'old-run',
  instanceId: '00000000000a',
  attachedAt: 1,
};
const listing: HostListing = {
  kind: 'listed',
  host: 'ssh:gpu',
  live: [],
  ended: [],
  unreadable: [],
  unverified: [],
};

test('an expired tombstone still opens a recent run from its summary', () => {
  const record = runRecord([listing], recent, recent);
  expect(record).toEqual({kind: 'historical', status: null});
  const initial = runPageInitial({
    identity: recent,
    title: {project: recent.project, task: recent.task},
    hostLabel: recent.hostLabel,
    unavailable: {},
  });
  const opened = runPageStep(initial, {type: 'record', record});
  expect(opened.state.summary.kind).toBe('reading');
  expect(opened.requests).toMatchObject([{kind: 'read-summary', status: 'stopped'}]);
});

test('uncertain host listings do not classify an old recent run as ended', () => {
  expect(runRecord([{...listing, unverified: [recent.instanceId ?? '']}], recent, recent)).toEqual({
    kind: 'unlisted',
  });
});
