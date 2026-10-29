import {expect, test} from 'bun:test';
import type {HostListing, HostRun} from '@vibesys/hosts';
import {type RecentRun, recentStatus} from './recent-status.js';

const at = (id: string, phase: 'running' | 'failed', startedAt: number): HostRun => ({
  id,
  projectRoot: '/home/me/p',
  runId: 'r1',
  status: phase,
  phase,
  paused: null,
  end: null,
  startedAt,
  reason: null,
});

const remembered: RecentRun = {
  host: 'ssh:gpu',
  hostLabel: 'gpu',
  project: '~/p',
  task: null,
  runId: 'r1',
  instanceId: '00000000000a',
  attachedAt: 1,
};

const listing = (live: HostRun[]): HostListing => ({
  kind: 'listed',
  host: 'ssh:gpu',
  live,
  ended: [at('00000000000a', 'failed', 10), at('00000000000b', 'failed', 20)],
  unreadable: [],
  unverified: [],
});

test('attach picks the live server among several of one run, whatever way its directory is remembered', () => {
  const status = recentStatus(remembered, listing([at('0000000000ff', 'running', 5)]));
  expect(status).toMatchObject({kind: 'live', instanceId: '0000000000ff'});
});

test('with no live server the latest ended one says how the run ended', () => {
  expect(recentStatus(remembered, listing([]))).toMatchObject({
    kind: 'ended',
    instanceId: '00000000000b',
  });
});
