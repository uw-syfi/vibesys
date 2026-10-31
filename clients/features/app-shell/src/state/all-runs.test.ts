import {describe, expect, test} from 'bun:test';
import type {HostListing, HostRun, ListedRun, RunDigestEntry} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type Clock,
  dashboardRows,
  projectRunGaps,
  runLabels,
  runsKnown,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  type SidebarStep,
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
const cancelsOf = (requests: readonly SidebarRequest[]) =>
  requests.flatMap(request => (request.kind === 'cancel-project-runs' ? [request.token] : []));
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
        {
          kind: 'unavailable',
          host: 'local',
          step: 'gateway',
          connection: {kind: 'offline', detail: 'down'},
        },
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

    expect(rows.map(each => [each.machine, each.row.run.runId])).toEqual([
      ['host-a', 'new'],
      ['host-b', 'solo'],
      ['host-a', 'mid'],
      ['host-a', 'old'],
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

    expect(rows.map(each => [each.row.run.runId, each.row.title])).toEqual([
      ['crashed', 'q · spsc'],
      ['unknown', 'q'],
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

  test('lists every run, however many, and reads the digest of each', () => {
    const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'seen')]);
    const many = Array.from({length: 230}, (_, index) => run(`r${index}`, index));
    const step = sidebarStep(
      state,
      listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', many),
    );

    const asked = digestsOf(step.requests);

    expect(asked.flatMap(request => request.runIds)).toEqual(many.map(each => each.runId));
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
    expect(rows.map(each => each.row.run.runId)).toEqual(many.map(each => each.runId));
    expect(rows.every(each => each.row.status === 'finished')).toBe(true);
    expect(sidebarInvariants(answered)).toEqual([]);
  });

  test('property: every run a machine lists is on the dashboard once, known project or not, however many', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const projects = Array.from({length: 1 + random.int(4)}, (_, index) => `/project-${index}`);
      const hostRuns = new Map(
        projects.map((project, index) => [
          project,
          Array.from({length: random.int(90)}, (_, id) => run(`${index}-${id}`, random.int(400))),
        ]),
      );
      const unknown = Array.from({length: random.int(90)}, (_, id) => ({
        ...server(`unknown-${id}`, '', 'completed'),
        startedAt: NOW / 1000 - random.int(400) * 3600,
      }));
      const withUnknown = {
        ...listed('ssh:host-a'),
        unknownProject: unknown.map(({projectRoot: _, ...each}) => each),
      };
      const first = sidebarStep(SIDEBAR_EMPTY, {
        type: 'overview',
        listings: [withUnknown],
        recent: projects.map(project => opened('ssh:host-a', project, 'remembered')),
        skippedRecent: [],
      });
      let step = first;
      for (const request of listRequests(first)) {
        step = sidebarStep(step.state, {
          type: 'project-runs-listed',
          token: request.token,
          host: request.host,
          project: request.project,
          runs: hostRuns.get(request.project) ?? [],
        });
      }
      const archived = [...hostRuns.values()]
        .flat()
        .filter(() => random.int(8) === 0)
        .map(each => ({host: 'ssh:host-a' as const, runId: each.runId}));
      const archivedState = sidebarStep(step.state, {type: 'archived', archived});
      const answered = digestsOf([
        ...first.requests,
        ...step.requests,
        ...archivedState.requests,
      ]).reduce(
        (current, request) =>
          sidebarStep(current, {
            type: 'digests-read',
            token: request.token,
            reply: {kind: 'digests', runs: new Map(request.runIds.map(id => [id, read()]))},
          }).state,
        archivedState.state,
      );

      const listedIds = [
        ...[...hostRuns.values()].flat().map(each => each.runId),
        ...unknown.map(each => each.runId),
      ].filter(id => !archived.some(each => each.runId === id));
      const shown = dashboardRows(answered, CLOCK).map(each => each.row.run.runId);
      expect([...shown].sort()).toEqual([...listedIds].sort());
      expect(sidebarInvariants(answered)).toEqual([]);
    }
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

  test('a run the machine reports is listed with no local record and whatever its project lists', () => {
    // A fresh profile: nothing opened, so no Recent record points at any run.
    for (const answer of [
      (): readonly ListedRun[] | null => [],
      (): readonly ListedRun[] | null => null,
      (): readonly ListedRun[] | null => [run('other', 3)],
    ]) {
      const {state, requests} = open([
        listed(
          'ssh:host-a',
          [server('live', '/p/q', 'running')],
          [server('done', '/p/q', 'completed')],
        ),
      ]);
      const answered = apply(state, [
        listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', answer()),
      ]);

      const rows = dashboardRows(answered, CLOCK);

      expect(rows.map(each => each.row.run.runId)).toContain('live');
      expect(rows.map(each => each.row.run.runId)).toContain('done');
      expect(rows.find(each => each.row.run.runId === 'live')?.row.status).toBe('live');
      expect(sidebarInvariants(answered)).toEqual([]);
    }
  });

  test('before any project listing answers, the live run is already listed once', () => {
    const {state} = open([listed('ssh:host-a', [server('live', '/p/q', 'running')])]);
    expect(dashboardRows(state, CLOCK).map(each => each.row.run.runId)).toEqual(['live']);
    // The project's own listing of it replaces the report, never doubles it.
    const requests = sidebarStep(state, {type: 'select', screen: {kind: 'new-run'}}).requests;
    expect(requests).toEqual([]);
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

  test('asking again cancels each listing still being read and no other', () => {
    const projects = ['/p/a', '/p/b', '/p/c'];
    const {state, requests} = open(
      [listed('ssh:host-a')],
      projects.map((project, index) => opened('ssh:host-a', project, `r${index}`)),
    );
    const [first, second, third] = listsOf(requests);
    // One listing answered, one failed, one is still being read.
    const mixed = apply(state, [
      listedAction(first?.[2] as number, 'ssh:host-a', first?.[1] as string, [run('x', 1)]),
      listedAction(second?.[2] as number, 'ssh:host-a', second?.[1] as string, null),
    ]);
    const away = sidebarStep(mixed, {type: 'select', screen: {kind: 'new-run'}});
    expect(cancelsOf(away.requests)).toEqual([]);

    const back = sidebarStep(away.state, {type: 'select', screen: {kind: 'runs'}});
    expect(cancelsOf(back.requests)).toEqual([third?.[2] as number]);
    // The cancelled token names nothing now, and the three projects are asked again under new ones.
    expect(listsOf(back.requests)).toHaveLength(3);
    for (const [, , token] of listsOf(back.requests)) {
      expect(cancelsOf(back.requests)).not.toContain(token);
    }
    // Staying on the screen asks nothing and cancels nothing.
    const stay = sidebarStep(back.state, {type: 'select', screen: {kind: 'runs'}});
    expect(stay.requests).toEqual([]);
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

describe('what every screen calls a run', () => {
  /** Two runs of `/p/queue-rs` listed with `task`: one the user opened (with `chosen`), one not. */
  function listedWith(task: string | null, chosen: string | null) {
    const {state, requests} = open(
      [listed('ssh:host-a', [server('live', '/p/queue-rs', 'running')])],
      [{...opened('ssh:host-a', '/p/queue-rs', 'seen'), task: chosen as string}],
    );
    return sidebarStep(
      state,
      listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/queue-rs', [
        {runId: 'live', name: 'live', task, createdAt: NOW - HOUR},
        {runId: 'seen', name: 'seen', task, createdAt: NOW - 2 * HOUR},
        {runId: 'other', name: 'other', task, createdAt: NOW - 3 * HOUR},
      ]),
    ).state;
  }

  test('is its project and the task the user chose, never a task the machine only lists', () => {
    // `default` is the one task of a project made from flags: nobody chose it.
    const rows = dashboardRows(listedWith('default', 'mpmc'), CLOCK);
    expect(rows.map(each => [each.row.run.runId, each.row.title])).toEqual([
      ['live', 'queue-rs'],
      ['seen', 'queue-rs · mpmc'],
      ['other', 'queue-rs'],
    ]);
  });

  test('titles a run by its project when its Recent record holds the synthesized task', () => {
    const rows = dashboardRows(listedWith('default', 'default'), CLOCK);
    expect(rows.map(each => each.row.title)).toEqual(['queue-rs', 'queue-rs', 'queue-rs']);
  });

  test('is worded the same by the Usage page as by the dashboard, with when each began', () => {
    const state = listedWith('default', 'mpmc');
    const label = runLabels(state, CLOCK);
    for (const each of dashboardRows(state, CLOCK)) {
      const mine = label({host: each.host, project: each.project, runId: each.row.run.runId});
      expect(mine.title).toBe(each.row.title);
      expect(mine.began).not.toBeNull();
    }
    // A run the dashboard does not list has a title and no start.
    expect(label({host: 'ssh:host-a', project: '/p/queue-rs', runId: 'gone'})).toEqual({
      title: 'queue-rs',
      began: null,
    });
  });
});

test('a native import refusal stays visible on the run row', () => {
  const {state, requests} = open([listed('ssh:host-a')], [opened('ssh:host-a', '/p/q', 'r')]);
  const answered = sidebarStep(
    state,
    listedAction(listsOf(requests)[0]?.[2] as number, 'ssh:host-a', '/p/q', [
      {...run('r', 1), unavailable: 'Pause this run before importing its history'},
    ]),
  ).state;
  const [row] = dashboardRows(answered, CLOCK);
  expect(row?.row.problem).toEqual({
    text: 'Pause this run before importing its history',
    retry: false,
  });
  expect(row?.row.detail).toBe('Pause this run before importing its history');
});

type ProjectRunsRequest = Extract<SidebarRequest, {kind: 'list-project-runs'}>;

function listRequests(step: {readonly requests: readonly SidebarRequest[]}): ProjectRunsRequest[] {
  return step.requests.filter(
    (request): request is ProjectRunsRequest => request.kind === 'list-project-runs',
  );
}

function visibleRunIds(state: SidebarState): Set<string> {
  return new Set(dashboardRows(state, CLOCK).map(row => row.row.run.runId));
}

/** Answer the pending listings in a random order, each failing or not; every gap stays named. */
function answerShuffled(
  random: Seeded,
  start: SidebarStep,
  hostRuns: ReadonlyMap<string, readonly ListedRun[]>,
): SidebarStep {
  let step = start;
  const pending = listRequests(step);
  const complete = new Set<string>();
  while (pending.length > 0) {
    // Latency is reply ordering, so there is no wall-clock wait or verdict.
    const request = pending.splice(random.int(pending.length), 1)[0];
    if (!request) throw new Error('missing request');
    const fails = random.bool();
    step = sidebarStep(step.state, {
      type: 'project-runs-listed',
      ...request,
      runs: fails ? null : (hostRuns.get(request.project) ?? []),
      ...(fails ? {error: `refused ${request.project}`} : {}),
    });
    if (!fails) complete.add(request.project);
    expectListedOrNamed(step.state, hostRuns, complete);
  }
  return step;
}

/** Each project's runs are visible once listed; a project not listed yet is named as a gap. */
function expectListedOrNamed(
  state: SidebarState,
  hostRuns: ReadonlyMap<string, readonly ListedRun[]>,
  complete: ReadonlySet<string>,
): void {
  const gaps = projectRunGaps(state.allRuns);
  const visible = visibleRunIds(state);
  for (const [project, rows] of hostRuns) {
    if (complete.has(project)) expect(rows.every(row => visible.has(row.runId))).toBe(true);
    else expect(gaps.some(gap => gap.project === project)).toBe(true);
  }
}

test('property: reordered project failures remain visible and retry to a complete listing', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const projects = Array.from({length: 1 + random.int(5)}, (_, index) => `/project-${index}`);
    const hostRuns = new Map(
      projects.map((project, index) => [
        project,
        Array.from({length: 1 + random.int(5)}, (_, id) => run(`${index}-${id}`, id)),
      ]),
    );
    const first = sidebarStep(SIDEBAR_EMPTY, {
      type: 'overview',
      listings: [listed('ssh:host-a')],
      recent: projects.map(project => opened('ssh:host-a', project, 'remembered')),
      skippedRecent: [],
    });
    let step = answerShuffled(random, first, hostRuns);
    const refused = projectRunGaps(step.state.allRuns);
    expect(
      refused.every(
        gap => gap.refusal?.kind === 'unreachable' && gap.refusal.detail.includes(gap.project),
      ),
    ).toBe(true);
    step = sidebarStep(step.state, {type: 'refresh-live-digests'});
    expect(projectRunGaps(step.state.allRuns).map(gap => gap.refusal)).toEqual(
      refused.map(gap => gap.refusal),
    );
    for (const request of listRequests(step)) {
      step = sidebarStep(step.state, {
        type: 'project-runs-listed',
        ...request,
        runs: hostRuns.get(request.project) ?? [],
      });
    }
    expect(projectRunGaps(step.state.allRuns)).toEqual([]);
    expect(visibleRunIds(step.state)).toEqual(
      new Set([...hostRuns.values()].flat().map(row => row.runId)),
    );
  }
});

/** A host whose link drops and returns while the dashboard reads; every request settles. */
class FlakyHost {
  up = true;
  pending: SidebarRequest[] = [];
  step: SidebarStep;

  constructor(
    readonly hostRuns: ReadonlyMap<string, readonly ListedRun[]>,
    host: 'ssh:host-a',
  ) {
    this.step = sidebarStep(SIDEBAR_EMPTY, {
      type: 'overview',
      listings: [listed(host)],
      recent: [...hostRuns.keys()].map(project => opened(host, project, 'remembered')),
      skippedRecent: [],
    });
    this.pending = [...this.step.requests];
  }

  #dispatch(action: SidebarAction): void {
    this.step = sidebarStep(this.step.state, action);
    this.pending.push(...this.step.requests);
  }

  answer(at: number): void {
    const request = this.pending.splice(at, 1)[0];
    if (!request) return;
    if (request.kind === 'cancel-project-runs') {
      // The host stops reading that listing, so its reply never comes.
      const reading = this.pending.findIndex(
        each => each.kind === 'list-project-runs' && each.token === request.token,
      );
      if (reading >= 0) this.pending.splice(reading, 1);
    } else if (request.kind === 'list-project-runs') {
      const runs = this.up ? (this.hostRuns.get(request.project) ?? []) : null;
      this.#dispatch({
        type: 'project-runs-listed',
        token: request.token,
        host: request.host,
        project: request.project,
        runs,
        ...(runs === null ? {error: 'host read deadline exceeded'} : {}),
      });
    } else {
      const found = new Map(request.runIds.map(id => [id, read()] as const));
      this.#dispatch({
        type: 'digests-read',
        token: request.token,
        reply: this.up ? {kind: 'digests', runs: found} : null,
      });
    }
  }

  link(up: boolean): void {
    this.up = up;
    this.#dispatch({
      type: 'listing',
      listing: up
        ? listed('ssh:host-a')
        : {
            kind: 'unavailable',
            host: 'ssh:host-a',
            step: 'gateway',
            connection: {kind: 'reconnecting', attempt: 1},
          },
    });
  }

  tick(): void {
    this.#dispatch({type: 'refresh-live-digests'});
  }

  /** One of: an answer (a failure while the link is down), a drop or return, a refresh tick. */
  randomly(random: Seeded): void {
    const choice = random.int(6);
    if (choice < 3 && this.pending.length > 0) this.answer(random.int(this.pending.length));
    else if (choice === 3) this.link(!this.up);
    else if (choice === 4) this.tick();
  }

  /** The link is back for good: each refresh and each answer is now a success. */
  returnForGood(): void {
    if (!this.up) this.link(true);
    for (let round = 0; round < 4; round++) {
      while (this.pending.length > 0) this.answer(0);
      this.tick();
    }
    while (this.pending.length > 0) this.answer(0);
  }
}

test('property: a link dropped at any point while reading converges to the host once it returns', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const projects = Array.from({length: 1 + random.int(4)}, (_, index) => `/project-${index}`);
    const hostRuns = new Map(
      projects.map((project, index) => [
        project,
        Array.from({length: 1 + random.int(4)}, (_, id) => run(`${index}-${id}`, id)),
      ]),
    );
    const world = new FlakyHost(hostRuns, 'ssh:host-a');
    let hadRuns = false;
    for (let step = 0; step < 40; step++) {
      world.randomly(random);
      // Once runs have been shown, no drop or return of the link makes the home anything but Runs
      // (the shell would leave for New run on `none`, or on `unknown` after its wait).
      const known = runsKnown(world.step.state, ['ssh:host-a']);
      if (hadRuns) expect(known).toBe('some');
      hadRuns ||= known === 'some';
    }
    world.returnForGood();
    const state = world.step.state;
    expect(projectRunGaps(state.allRuns)).toEqual([]);
    expect(visibleRunIds(state)).toEqual(
      new Set([...hostRuns.values()].flat().map(row => row.runId)),
    );
    // No card says "can't read run state" or "reading" once the host answers.
    const rows = dashboardRows(state, CLOCK);
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.filter(each => each.row.problem !== null || each.row.digest === null)).toEqual([]);
  }
});
