import {describe, expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun, SkippedRecent} from '@vibesys/runs';
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

  test('the disk read reports its skipped entries until a published list replaces it', () => {
    const feed = new WelcomeFeed();
    const skipped = [{index: 2, reason: 'recent.json: runs[2].task must be a task name'}];

    expect(feed.snapshot(values([recent('a')], skipped)).skippedRecent).toEqual(skipped);

    feed.recent([recent('b')]);
    expect(feed.snapshot(values([recent('a')], skipped)).skippedRecent).toEqual([]);
  });

  test('a retired host is absent from later renderer snapshots', () => {
    const feed = new WelcomeFeed();
    feed.listing(listing('ssh:worker', 'obsolete'));

    feed.retireHost('ssh:worker');

    expect(feed.snapshot(values([])).listings).toEqual([]);
  });
});

function values(recentRuns: readonly RecentRun[], skippedRecent: readonly SkippedRecent[] = []) {
  return {recent: recentRuns, skippedRecent};
}

function recent(runId: string): RecentRun {
  return {
    host: 'local',
    hostLabel: 'This Mac',
    project: `/projects/${runId}`,
    task: 'queue',
    runId,
    instanceId: `instance-${runId}`,
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
        phase: 'running',
        paused: null,
        end: null,
        startedAt: 1,
        reason: null,
      },
    ],
    ended: [],
    unreadable: [],
    unverified: [],
  };
}
