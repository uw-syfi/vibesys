import {expect, test} from 'bun:test';
import type {HostListing, HostRun} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {runRecord} from './record.js';

const server = (id: string, phase: 'running' | 'failed', startedAt: number): HostRun => ({
  id,
  projectRoot: '/home/me/p',
  runId: 'r1',
  status: phase,
  phase,
  paused: null,
  end: phase === 'running' ? null : {outcome: 'failed', reason: 'exited', resumable: false},
  startedAt,
  reason: null,
});

const recent: RecentRun = {
  host: 'ssh:gpu',
  hostLabel: 'gpu',
  project: '/home/me/p',
  task: null,
  runId: 'r1',
  instanceId: '00000000000a',
  attachedAt: 1,
};

const ended = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'].map((letter, at) =>
  server(`00000000000${letter}`, 'failed', 100 + at),
);

const world = (live: HostRun[]): HostListing => ({
  kind: 'listed',
  host: 'ssh:gpu',
  live,
  ended,
  unreadable: [],
  unverified: [],
});

// The app remembers the directory as the user typed it; the registry holds it resolved.
const SPELLINGS = ['/home/me/p', '~/p', '/home/me/p/'];

test('a run resumed many times opens on its live server, however its directory is remembered', () => {
  for (const project of SPELLINGS) {
    const record = runRecord([world([server('0000000000ff', 'running', 50)])], recent, {
      ...recent,
      project,
    });
    expect(record).toMatchObject({kind: 'live', run: {id: '0000000000ff'}});
  }
});

test('with no live server the newest ended one speaks, however its directory is remembered', () => {
  for (const project of SPELLINGS) {
    const record = runRecord([world([])], recent, {...recent, project});
    expect(record).toMatchObject({kind: 'ended', run: {id: '00000000000h'}});
  }
});
