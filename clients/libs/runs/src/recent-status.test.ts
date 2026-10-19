import {describe, expect, test} from 'bun:test';
import type {HostListing, HostRun} from '@vibesys/hosts';
import {type RecentRun, recentRunKey} from '@vibesys/runs';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {knownProjects, recentStatus} from './recent-status.js';

const RECENT: RecentRun = {
  host: 'ssh:gpu',
  hostLabel: 'gpu',
  project: '/p',
  task: 'spsc',
  runId: 'run-1',
  instanceId: '00000000000a',
  attachedAt: 1,
};

function server(id: number, startedAt: number, status: string, runId = 'run-1'): HostRun {
  return {
    id: id.toString(16).padStart(12, '0'),
    projectRoot: '/p',
    runId,
    status,
    phase: 'running',
    paused: null,
    end: null,
    startedAt,
    reason: null,
  };
}

describe('recentStatus', () => {
  test('recent identity includes the project that scopes a run id', () => {
    const reattached: RecentRun = {...RECENT, attachedAt: 99};

    expect(recentRunKey(RECENT)).not.toBe(recentRunKey({...RECENT, project: '/another-project'}));
    expect(recentRunKey(RECENT)).toBe(recentRunKey(reattached));
  });

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
        instanceId: latest?.id ?? null,
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

  test.each([
    'unverified',
    'unreadable',
  ] as const)('a recent run whose current instance is %s stays unknown instead of offering Resume', uncertain => {
    const listing: HostListing = {
      kind: 'listed',
      host: 'ssh:gpu',
      live: [],
      // An older server for this logical run is not evidence that its current server ended.
      ended: [server(1, 1, 'stopped')],
      unreadable: uncertain === 'unreadable' ? [RECENT.instanceId ?? ''] : [],
      unverified: uncertain === 'unverified' ? [RECENT.instanceId ?? ''] : [],
    };

    expect(recentStatus(RECENT, listing)).toEqual({kind: 'unknown'});
    expect(
      recentStatus(RECENT, {
        ...listing,
        live: [server(10, 10, 'running')],
        unreadable: [],
        unverified: [],
      }),
    ).toEqual({kind: 'live', instanceId: '00000000000a', status: 'running'});
  });

  test.each([
    'unverified',
    'unreadable',
  ] as const)('a migrated recent run without an instance id stays unknown while any row is %s', uncertain => {
    const migrated: RecentRun = {...RECENT, instanceId: null};
    const listing: HostListing = {
      kind: 'listed',
      host: 'ssh:gpu',
      live: [],
      ended: [server(1, 1, 'stopped')],
      unreadable: uncertain === 'unreadable' ? ['ffffffffffff'] : [],
      unverified: uncertain === 'unverified' ? ['ffffffffffff'] : [],
    };

    expect(recentStatus(migrated, listing)).toEqual({kind: 'unknown'});
  });

  test('a migrated recent run can be considered ended when the listing has no uncertainty', () => {
    const migrated: RecentRun = {...RECENT, instanceId: null};
    const ended = server(1, 1, 'stopped');
    const listing: HostListing = {
      kind: 'listed',
      host: 'ssh:gpu',
      live: [],
      ended: [ended],
      unreadable: [],
      unverified: [],
    };

    expect(recentStatus(migrated, listing)).toEqual({
      kind: 'ended',
      instanceId: ended.id,
      status: 'stopped',
      reason: null,
    });
  });

  test('an ended run keeps its durable locator after its tombstone expires', () => {
    const other = server(9, 9, 'failed');
    const listing: HostListing = {
      kind: 'listed',
      host: 'ssh:gpu',
      live: [],
      ended: [{...other, projectRoot: '/another-project'}],
      unreadable: [],
      unverified: [],
    };

    expect(recentStatus(RECENT, listing)).toEqual({
      kind: 'ended',
      instanceId: '00000000000a',
      status: null,
      reason: null,
    });
  });
});

describe('knownProjects', () => {
  test('names each project once: live servers, ended ones, then recent runs on the host', () => {
    const at = (projectRoot: string, id: number): HostRun => ({
      ...server(id, id, 'running'),
      projectRoot,
    });
    const listing: HostListing = {
      kind: 'listed',
      host: 'ssh:gpu',
      live: [at('/a', 1), at('/b', 2)],
      ended: [at('/b', 3), at('/c', 4), at('', 5)],
      unreadable: [],
      unverified: [],
    };
    const recent: RecentRun[] = [
      {...RECENT, project: '/d'},
      {...RECENT, host: 'local', project: '/elsewhere'},
      {...RECENT, project: '/a'},
    ];
    expect(knownProjects(listing, recent, 'ssh:gpu')).toEqual(['/a', '/b', '/c', '/d']);
    expect(knownProjects(undefined, recent, 'local')).toEqual(['/elsewhere']);
  });
});
