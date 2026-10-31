import {expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {runRecord, sameRecord} from './record.js';
import {type RunRecord, runPageInitial, runPageStep} from './run-page.js';

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
    why: 'unconfirmed',
  });
});

test('a machine that will not answer is named, not searched forever; one still connecting is not', () => {
  const unavailable = (connection: Extract<HostListing, {kind: 'unavailable'}>['connection']) =>
    runRecord(
      [{kind: 'unavailable', host: 'ssh:gpu', step: 'gateway', connection}],
      recent,
      recent,
    );
  expect(unavailable({kind: 'incompatible', detail: 'x'})).toEqual({
    kind: 'unlisted',
    why: 'machine',
    block: 'needs-update',
  });
  expect(unavailable({kind: 'auth-needed', detail: 'x'})).toEqual({
    kind: 'unlisted',
    why: 'machine',
    block: 'sign-in',
  });
  expect(unavailable({kind: 'offline', detail: 'x'})).toEqual({
    kind: 'unlisted',
    why: 'machine',
    block: 'offline',
  });
  expect(unavailable({kind: 'closed'})).toEqual({
    kind: 'unlisted',
    why: 'machine',
    block: 'closed',
  });
  for (const waiting of [{kind: 'connecting'}, {kind: 'reconnecting', attempt: 1}] as const) {
    expect(unavailable(waiting)).toEqual({kind: 'unlisted', why: 'machine'});
  }
  const offline: RunRecord = {kind: 'unlisted', why: 'machine', block: 'offline'};
  expect(sameRecord({kind: 'unlisted', why: 'machine'}, offline)).toBe(false);
  expect(sameRecord(offline, {...offline})).toBe(true);
});
