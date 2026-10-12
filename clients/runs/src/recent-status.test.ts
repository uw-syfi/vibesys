import {describe, expect, test} from 'bun:test';
import type {HostListing, HostRun} from '@vibesys/hosts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import type {RecentRun} from './recent-status.js';
import {recentStatus} from './recent-status.js';

const RECENT: RecentRun = {
  host: 'ssh:gpu',
  hostLabel: 'gpu',
  project: '/p',
  task: 'spsc',
  runId: 'run-1',
  attachedAt: 1,
};

function server(id: number, startedAt: number, status: string, runId = 'run-1'): HostRun {
  return {
    id: id.toString(16).padStart(12, '0'),
    projectRoot: '/p',
    runId,
    status,
    startedAt,
    reason: null,
  };
}

describe('recentStatus', () => {
  test('regression: a run resumed and ended again reads as its latest server ended, in any order', () => {
    for (const seed of SEEDS.slice(0, 50)) {
      const random = new Seeded(seed);
      const ended = Array.from({length: 1 + random.int(4)}, (_, index) =>
        server(index + 1, index + 1, random.pick(['failed', 'stopped', 'completed'])),
      );
      const latest = ended.at(-1);
      const shuffled = [...ended].sort(() => random.next() - 0.5);
      const listing: HostListing = {
        kind: 'listed',
        host: 'ssh:gpu',
        live: [],
        ended: shuffled,
        unreadable: [],
        unverified: [],
      };
      expect(recentStatus(RECENT, listing)).toEqual({
        kind: 'ended',
        status: latest?.status ?? null,
        reason: null,
      });
    }
  });

  test('a live server driving the run in its project wins over any tombstone', () => {
    const listing: HostListing = {
      kind: 'listed',
      host: 'ssh:gpu',
      live: [server(9, 9, 'running')],
      ended: [server(1, 1, 'stopped')],
      unreadable: [],
      unverified: [],
    };
    expect(recentStatus(RECENT, listing)).toEqual({
      kind: 'live',
      instanceId: '000000000009',
      status: 'running',
    });
    expect(recentStatus(RECENT, undefined)).toEqual({kind: 'unknown'});
  });
});
