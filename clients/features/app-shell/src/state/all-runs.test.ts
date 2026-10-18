import {describe, expect, test} from 'bun:test';
import type {HostListing, HostRun, ListedRun, RunDigestEntry} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {
  allRunsGroups,
  type Clock,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  sidebarInvariants,
  sidebarStep,
} from './index.js';

const NOW = Date.parse('2026-10-17T12:00:00Z');
const CLOCK: Clock = {now: NOW, timeZone: 'UTC'};
const HOUR = 3_600_000;

const listed = (host: 'ssh:host-a' | 'ssh:host-b', live: HostRun[] = [], ended: HostRun[] = []) =>
  ({kind: 'listed', host, live, ended, unreadable: [], unverified: []}) satisfies HostListing;

const server = (runId: string, project: string, phase: HostRun['phase']): HostRun => ({
  id: '000000000001',
  projectRoot: project,
  runId,
  status: phase,
  phase,
  paused: null,
  end: null,
  startedAt: NOW / 1000 - 5,
  reason: null,
});

const opened = (host: 'ssh:host-a' | 'ssh:host-b', project: string, runId: string): RecentRun => ({
  host,
  hostLabel: host,
  project,
  task: 'mpmc',
  runId,
  instanceId: null,
  attachedAt: 1,
});

const run = (runId: string, hoursAgo: number, task: string | null = 'mpmc'): ListedRun => ({
  runId,
  task,
  createdAt: NOW - hoursAgo * HOUR,
});

const read = (over: Partial<Extract<RunDigestEntry, {kind: 'read'}>> = {}): RunDigestEntry => ({
  kind: 'read',
  createdAt: NOW - 2 * HOUR,
  ideas: 4,
  adoptedRatio: 2,
  durationSeconds: 600,
  outcome: 'adopted',
  closing: 'finished',
  over: true,
  ...over,
});

const listsOf = (requests: readonly SidebarRequest[]) =>
  requests.flatMap(request =>
    request.kind === 'list-project-runs' ? [[request.host, request.project, request.token]] : [],
  );
const digestsOf = (requests: readonly SidebarRequest[]) =>
  requests.flatMap(request => (request.kind === 'read-digests' ? [request] : []));

/** The state with `listings` and `recent` known and the All runs screen open, and what it asked. */
function open(listings: readonly HostListing[], recent: readonly RecentRun[] = []) {
  const known = sidebarStep(SIDEBAR_EMPTY, {type: 'overview', listings, recent, skippedRecent: []});
  return sidebarStep(known.state, {type: 'select', screen: {kind: 'all-runs'}});
}

function apply(state: SidebarState, actions: readonly SidebarAction[]): SidebarState {
  return actions.reduce((current, action) => sidebarStep(current, action).state, state);
}

describe('the All runs screen', () => {
  test('asks each connected machine for every project it has met there, once', () => {
    const {state, requests} = open(
      [
        listed(
          'ssh:host-a',
          [server('r1', '/p/queue-rs', 'running')],
          [server('r2', '/p/kv', 'completed')],
        ),
        listed('ssh:host-b'),
        {kind: 'unavailable', host: 'local', connection: {kind: 'offline', detail: 'down'}},
      ],
      [opened('ssh:host-a', '/p/queue-rs', 'r1'), opened('ssh:host-b', '/p/old', 'r9')],
    );

    expect(listsOf(requests).map(([host, project]) => [host, project])).toEqual([
      ['ssh:host-a', '/p/queue-rs'],
      ['ssh:host-a', '/p/kv'],
      ['ssh:host-b', '/p/old'],
    ]);
    // The same listings again ask for nothing: each project is already loading.
    const again = sidebarStep(state, {type: 'listing', listing: listed('ssh:host-a')});
    expect(listsOf(again.requests)).toEqual([]);
  });

  test('lists every run grouped by machine, newest first, each row opening its run', () => {
    const {state, requests} = open(
      [listed('ssh:host-a'), listed('ssh:host-b')],
      [opened('ssh:host-a', '/p/queue-rs', 'seen-a'), opened('ssh:host-b', '/p/kv', 'seen-b')],
    );
    const [first, second] = listsOf(requests);
    const answered = apply(state, [
      {
        type: 'project-runs-listed',
        token: first?.[2] as number,
        host: 'ssh:host-a',
        project: '/p/queue-rs',
        runs: [run('new', 1), run('mid', 3), run('old', 5, null)],
      },
      {
        type: 'project-runs-listed',
        token: second?.[2] as number,
        host: 'ssh:host-b',
        project: '/p/kv',
        runs: [run('solo', 2)],
      },
    ]);

    const groups = allRunsGroups(answered, CLOCK);

    expect(
      groups.map(group => [group.title, group.state, group.rows.map(row => row.run.runId)]),
    ).toEqual([
      ['host-a', 'listed', ['new', 'mid', 'old']],
      ['host-b', 'listed', ['solo']],
    ]);
    expect(groups[0]?.rows.map(row => row.run.host)).toEqual([
      'ssh:host-a',
      'ssh:host-a',
      'ssh:host-a',
    ]);
    expect(groups[0]?.rows[0]?.title).toBe('queue-rs · mpmc');
    expect(groups[0]?.rows[2]?.title).toBe('queue-rs');
    expect(sidebarInvariants(answered)).toEqual([]);
  });

  test('reads the digest of every listed run in requests of at most a hundred', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'seen')]);
    const many = Array.from({length: 230}, (_, index) => run(`r${index}`, index));
    const step = sidebarStep(state, {
      type: 'project-runs-listed',
      token: listsOf(requests)[0]?.[2] as number,
      host: 'ssh:host-a',
      project: '/p/q',
      runs: many,
    });

    const asked = digestsOf(step.requests);

    expect(asked.map(request => request.runIds.length)).toEqual([100, 100, 30]);
    expect(new Set(asked.flatMap(request => request.runIds)).size).toBe(230);
    const answered = asked.reduce(
      (current, request) =>
        sidebarStep(current, {
          type: 'digests-read',
          token: request.token,
          reply: {kind: 'digests', runs: new Map(request.runIds.map(id => [id, read()]))},
        }).state,
      step.state,
    );
    const [group] = allRunsGroups(answered, CLOCK);
    expect(group?.rows).toHaveLength(230);
    expect(group?.rows.every(row => row.status === 'finished')).toBe(true);
  });

  test('a live run shows as live, and a run the host has no record of is gone', () => {
    const {state, requests} = open([listed('ssh:host-a', [server('live', '/p/q', 'running')])]);
    const step = sidebarStep(state, {
      type: 'project-runs-listed',
      token: listsOf(requests)[0]?.[2] as number,
      host: 'ssh:host-a',
      project: '/p/q',
      runs: [run('live', 1), run('ghost', 2)],
    });
    const asked = digestsOf(step.requests);
    const answered = sidebarStep(step.state, {
      type: 'digests-read',
      token: asked[0]?.token as number,
      reply: {kind: 'digests', runs: new Map()},
    }).state;

    const [group] = allRunsGroups(answered, CLOCK);

    expect(group?.rows.map(row => [row.run.runId, row.status])).toEqual([
      ['live', 'live'],
      ['ghost', 'unknown'],
    ]);
    expect(group?.rows[1]?.problem).toEqual({text: 'this run is gone', retry: false});
  });

  test('a failed listing is said, a late reply is dropped, and Reload asks again', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r0')]);
    const [token] = [listsOf(requests)[0]?.[2] as number];
    const stale = sidebarStep(state, {
      type: 'project-runs-listed',
      token: token + 7,
      host: 'ssh:host-a',
      project: '/p/q',
      runs: [run('x', 1)],
    }).state;
    expect(allRunsGroups(stale, CLOCK)[0]?.state).toBe('loading');

    const failed = apply(state, [
      {type: 'project-runs-listed', token, host: 'ssh:host-a', project: '/p/q', runs: null},
    ]);
    expect(allRunsGroups(failed, CLOCK)[0]?.state).toBe('failed');

    const reloaded = sidebarStep(failed, {type: 'reload-all-runs'});
    expect(listsOf(reloaded.requests).map(([host, project]) => [host, project])).toEqual([
      ['ssh:host-a', '/p/q'],
    ]);
  });

  test('leaving and opening the screen again lists afresh, and other screens ask nothing', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r0')]);
    expect(listsOf(requests)).toHaveLength(1);
    const away = sidebarStep(state, {type: 'select', screen: {kind: 'new-run'}});
    expect(listsOf(away.requests)).toEqual([]);
    const back = sidebarStep(away.state, {type: 'select', screen: {kind: 'all-runs'}});
    expect(listsOf(back.requests)).toHaveLength(1);
  });

  test('newest first holds for any creation times, and every listed run appears once', () => {
    for (let seed = 1; seed <= 40; seed += 1) {
      const times = Array.from(
        {length: seed % 9},
        (_, index) => ((seed * 7919 + index * 104729) % 50) * HOUR,
      );
      const runs = times.map((time, index) => ({
        runId: `r${index}`,
        task: null,
        createdAt: NOW - time,
      }));
      const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r0')]);
      const answered = sidebarStep(state, {
        type: 'project-runs-listed',
        token: listsOf(requests)[0]?.[2] as number,
        host: 'ssh:host-a',
        project: '/p/q',
        runs,
      }).state;

      const rows = allRunsGroups(answered, CLOCK)[0]?.rows ?? [];
      const created = rows.map(
        row => runs.find(each => each.runId === row.run.runId)?.createdAt as number,
      );

      expect(created).toEqual([...created].sort((left, right) => right - left));
      expect(rows.map(row => row.run.runId).sort()).toEqual(runs.map(each => each.runId).sort());
    }
  });

  test('machines are ordered by their newest run, whatever the listing order', () => {
    const HOSTS = ['ssh:host-a', 'ssh:host-b', 'local'] as const;
    for (let seed = 1; seed <= 60; seed += 1) {
      const newest = HOSTS.map((_, index) =>
        seed % (index + 4) === 0 ? null : ((seed * 31 + index * 17) % 40) * HOUR,
      );
      const order = [...HOSTS.keys()].sort(
        (a, b) => ((seed >> a) & 1) - ((seed >> b) & 1) || a - b,
      );
      const listings = order.map(index => listed(HOSTS[index] as 'ssh:host-a'));
      const {state, requests} = open(
        listings,
        order.map(index => opened(HOSTS[index] as 'ssh:host-a', '/p/q', 'r0')),
      );
      const answered = apply(
        state,
        listsOf(requests).map(([host, project, token]) => {
          const age = newest[HOSTS.indexOf(host as (typeof HOSTS)[number])] ?? null;
          return {
            type: 'project-runs-listed',
            token: token as number,
            host: host as 'ssh:host-a',
            project: project as string,
            runs: age === null ? [] : [run('old', age + 5), run('new', age)],
          } satisfies SidebarAction;
        }),
      );

      const groups = allRunsGroups(answered, CLOCK);
      const ages = groups.map(
        group => newest[HOSTS.indexOf(group.host as (typeof HOSTS)[number])] ?? null,
      );

      // Newest first (smallest age), the machines with no run last, ties in listing order.
      const rank = ages.map(age => (age === null ? Number.POSITIVE_INFINITY : age));
      expect(rank).toEqual([...rank].sort((left, right) => left - right));
      expect(groups.map(group => group.host).sort()).toEqual([...HOSTS].sort());
      for (const group of groups) {
        expect(group.rows.map(row => row.run.runId)).toEqual(
          group.rows.length === 0 ? [] : ['new', 'old'],
        );
      }
    }
  });
});
