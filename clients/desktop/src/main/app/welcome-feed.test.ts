import {describe, expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import type {ChromeState} from '@vibesys/ipc';
import type {RecentRun} from '@vibesys/runs';
import {WelcomeFeed} from './welcome-feed.js';

describe('WelcomeFeed', () => {
  test('snapshots the latest listing even when its push predates the renderer', () => {
    const feed = new WelcomeFeed();
    const first = feed.listing(listing('ssh:worker', 'old'));
    const second = feed.listing(listing('ssh:worker', 'current'));
    feed.listing(listing('local', 'local-run'));

    const snapshot = feed.snapshot(values([]));
    expect(first.sequence).toBeLessThan(second.sequence);
    expect(snapshot.eventCursor).toBe(3);
    expect(snapshot.listings).toEqual([
      listing('ssh:worker', 'current'),
      listing('local', 'local-run'),
    ]);
  });

  test('the final cursor covers a Recent event already reflected in the snapshot', () => {
    const feed = new WelcomeFeed();
    const published = feed.recent([recent('current')]);

    const snapshot = feed.snapshot(values([recent('stale-read')]));
    expect(snapshot.eventCursor).toBe(published.sequence);
    expect(snapshot.recent).toEqual([recent('current')]);
  });
});

function values(recentRuns: readonly RecentRun[]) {
  return {
    settingsProblem: null,
    recent: recentRuns,
    chrome: chrome('snapshot'),
    host: 'local' as const,
  };
}

function chrome(runId: string): ChromeState {
  return {
    mode: 'run',
    attached: {
      host: 'local',
      hostLabel: 'This Mac',
      instanceId: `instance-${runId}`,
      runId,
      project: `/projects/${runId}`,
      connection: {kind: 'connected'},
    },
    stops: {},
  };
}

function recent(runId: string): RecentRun {
  return {
    host: 'local',
    hostLabel: 'This Mac',
    project: `/projects/${runId}`,
    task: 'queue',
    runId,
    attachedAt: 1,
  };
}

function listing(host: 'local' | 'ssh:worker', runId: string): HostListing {
  return {
    kind: 'listed',
    host,
    live: [
      {
        id: `instance-${runId}`,
        projectRoot: `/projects/${runId}`,
        runId,
        status: 'running',
        startedAt: 1,
        reason: null,
      },
    ],
    ended: [],
    unreadable: [],
  };
}
