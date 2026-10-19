import {describe, expect, test} from 'bun:test';
import type {HostListing, HostRun, ListedRun, RunDigestEntry} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {
  type Clock,
  DASHBOARD_RUNS,
  dashboardRows,
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
  name: runId,
  task,
  createdAt: NOW - hoursAgo * HOUR,
});

const read = (over: Partial<Extract<RunDigestEntry, {kind: 'read'}>> = {}): RunDigestEntry => ({
  kind: 'read',
  createdAt: NOW - 2 * HOUR,
  ideas: 4,
  adoptedRatio: 2,
  bestSoFarRatio: null,
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

/** The state with `listings` and `recent` known, and what the dashboard (the home screen) asked. */
function open(listings: readonly HostListing[], recent: readonly RecentRun[] = []) {
  return sidebarStep(SIDEBAR_EMPTY, {type: 'overview', listings, recent, skippedRecent: []});
}

function apply(state: SidebarState, actions: readonly SidebarAction[]): SidebarState {
  return actions.reduce((current, action) => sidebarStep(current, action).state, state);
}

const listedAction = (
  token: number,
  host: 'ssh:host-a' | 'ssh:host-b',
  project: string,
  runs: readonly ListedRun[] | null,
): SidebarAction => ({type: 'project-runs-listed', token, host, project, runs});

describe('the Runs dashboard', () => {
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

  test('lists the runs of every machine newest first, each row opening its run', () => {
    const {state, requests} = open(
      [listed('ssh:host-a'), listed('ssh:host-b')],
      [opened('ssh:host-a', '/p/queue-rs', 'seen-a'), opened('ssh:host-b', '/p/kv', 'seen-b')],
    );
    const [first, second] = listsOf(requests);
    const answered = apply(state, [
      listedAction(first?.[2] as number, 'ssh:host-a', '/p/queue-rs', [
        run('new', 1),
        run('mid', 3),
        run('old', 5, null),
      ]),
      listedAction(second?.[2] as number, 'ssh:host-b', '/p/kv', [run('solo', 2)]),
    ]);

    const rows = dashboardRows(answered, CLOCK);

    expect(rows.map(each => [each.machine, each.row.run.runId, each.task])).toEqual([
      ['host-a', 'new', 'mpmc'],
      ['host-b', 'solo', 'mpmc'],
      ['host-a', 'mid', 'mpmc'],
      ['host-a', 'old', null],
    ]);
    expect(rows[0]?.row.run.host).toBe('ssh:host-a');
    expect(sidebarInvariants(answered)).toEqual([]);
  });

  test('names a run the machine lists without a task by the task its Recent record has', () => {
    const {state, requests} = open(
      [listed('ssh:host-a')],
      [{...opened('ssh:host-a', '/p/q', 'crashed'), task: 'spsc'}],
    );
    const answered = sidebarStep(
      state,
      listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', [
        run('crashed', 1, null),
        run('unknown', 2, null),
      ]),
    ).state;

    const rows = dashboardRows(answered, CLOCK);

    expect(rows.map(each => [each.row.run.runId, each.task])).toEqual([
      ['crashed', 'spsc'],
      ['unknown', null],
    ]);
  });

  test('says why a failed run ended from its journal, which the registry no longer holds', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r')]);
    const step = sidebarStep(
      state,
      listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', [run('boom', 1)]),
    );
    const asked = digestsOf(step.requests);
    const answered = sidebarStep(step.state, {
      type: 'digests-read',
      token: asked[0]?.token as number,
      reply: {
        kind: 'digests',
        runs: new Map([
          ['boom', read({outcome: 'failed', closing: 'failed', endSummary: 'the agent crashed'})],
        ]),
      },
    }).state;

    const [row] = dashboardRows(answered, CLOCK);

    expect(row?.row.status).toBe('failed');
    expect(row?.row.now).toBe('the agent crashed');
  });

  test('shows only the newest runs, and reads the digest of those alone', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'seen')]);
    const many = Array.from({length: 230}, (_, index) => run(`r${index}`, index));
    const step = sidebarStep(
      state,
      listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', many),
    );

    const asked = digestsOf(step.requests);

    expect(asked.map(request => request.runIds.length)).toEqual([DASHBOARD_RUNS]);
    const answered = asked.reduce(
      (current, request) =>
        sidebarStep(current, {
          type: 'digests-read',
          token: request.token,
          reply: {kind: 'digests', runs: new Map(request.runIds.map(id => [id, read()]))},
        }).state,
      step.state,
    );
    const rows = dashboardRows(answered, CLOCK);
    expect(rows).toHaveLength(DASHBOARD_RUNS);
    expect(rows.map(each => each.row.run.runId)).toEqual(
      many.slice(0, DASHBOARD_RUNS).map(each => each.runId),
    );
    expect(rows.every(each => each.row.status === 'finished')).toBe(true);
    expect(sidebarInvariants(answered)).toEqual([]);
  });

  test('a live run shows as live, and a run the host has no record of is gone', () => {
    const {state, requests} = open([listed('ssh:host-a', [server('live', '/p/q', 'running')])]);
    const step = sidebarStep(
      state,
      listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', [
        run('live', 1),
        run('ghost', 2),
      ]),
    );
    const asked = digestsOf(step.requests);
    const answered = sidebarStep(step.state, {
      type: 'digests-read',
      token: asked[0]?.token as number,
      reply: {kind: 'digests', runs: new Map()},
    }).state;

    const rows = dashboardRows(answered, CLOCK);

    expect(rows.map(each => [each.row.run.runId, each.row.status])).toEqual([
      ['live', 'live'],
      ['ghost', 'unknown'],
    ]);
    expect(rows[1]?.row.problem).toEqual({text: 'this run is gone', retry: false});
    expect(rows[0]?.row.startedAt).not.toBeNull();
  });

  test('an archived run is not shown', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r0')]);
    const answered = apply(state, [
      listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', [
        run('a', 1),
        run('b', 2),
      ]),
      {type: 'archived', archived: [{host: 'ssh:host-a', runId: 'a'}]},
    ]);
    expect(dashboardRows(answered, CLOCK).map(each => each.row.run.runId)).toEqual(['b']);
  });

  test('a late reply is dropped, and a failed listing leaves the runs shown before it', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r0')]);
    const token = listsOf(requests)[0]?.[2] as number;
    const stale = apply(state, [listedAction(token + 7, 'ssh:host-a', '/p/q', [run('x', 1)])]);
    expect(dashboardRows(stale, CLOCK)).toEqual([]);

    const listedOnce = apply(state, [listedAction(token, 'ssh:host-a', '/p/q', [run('x', 1)])]);
    const away = sidebarStep(listedOnce, {type: 'select', screen: {kind: 'new-run'}});
    const back = sidebarStep(away.state, {type: 'select', screen: {kind: 'runs'}});
    // Entering the dashboard lists again, and the runs of the last listing stay until it answers.
    expect(listsOf(back.requests).map(([host, project]) => [host, project])).toEqual([
      ['ssh:host-a', '/p/q'],
    ]);
    expect(dashboardRows(back.state, CLOCK).map(each => each.row.run.runId)).toEqual(['x']);
    const failed = apply(back.state, [
      listedAction(listsOf(back.requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', null),
    ]);
    expect(dashboardRows(failed, CLOCK).map(each => each.row.run.runId)).toEqual(['x']);
  });

  test('newest first holds for any creation times, and every listed run appears once', () => {
    for (let seed = 1; seed <= 40; seed += 1) {
      const times = Array.from(
        {length: seed % 9},
        (_, index) => ((seed * 7919 + index * 104729) % 50) * HOUR,
      );
      const runs = times.map((time, index) => ({
        runId: `r${index}`,
        name: `r${index}`,
        task: null,
        createdAt: NOW - time,
      }));
      const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r0')]);
      const answered = sidebarStep(
        state,
        listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', runs),
      ).state;

      const rows = dashboardRows(answered, CLOCK);
      const created = rows.map(each => each.createdAt);

      expect(created).toEqual([...created].sort((left, right) => right - left));
      expect(rows.map(each => each.row.run.runId).sort()).toEqual(
        runs.map(each => each.runId).sort(),
      );
    }
  });
});
