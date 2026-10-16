import {describe, expect, test} from 'bun:test';
import type {HostKey, HostListing, HostRun} from '@vibesys/hosts';
import type {RecentRun, RunRef} from '@vibesys/runs';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  initialSidebar,
  liveRows,
  machineCounts,
  RECENT_ROWS,
  type RunRowStatus,
  recentRows,
  runNotices,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  sidebarInvariants,
  sidebarStep,
} from './index.js';

const HOSTS: readonly HostKey[] = ['local', 'ssh:chelan4', 'ssh:gpu'];
const RUN_IDS = Array.from(
  {length: 30},
  (_, index) => `20261015-1600${String(index).padStart(2, '0')}`,
);
const PHASES = ['running', 'paused', 'starting', 'completed', 'failed', 'stopped'] as const;

/** A deterministic stream of numbers in [0, 1), so a property's cases are the same on every run. */
function seeded(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1103515245 + 12345) % 2147483648;
    return state / 2147483648;
  };
}

function pick<T>(random: () => number, values: readonly T[]): T {
  return values[Math.floor(random() * values.length)] as T;
}

function server(id: number, runId: string, phase: HostRun['phase'], startedAt = id): HostRun {
  return {
    id: id.toString(16).padStart(12, '0'),
    projectRoot: '~/projects/queue-rs',
    runId,
    status: phase,
    phase,
    paused: phase === 'paused' ? 'quota' : null,
    // A failed run records its end; the others are older tombstones that fall back to the phase.
    end:
      phase === 'failed'
        ? {
            outcome: 'failed',
            reason: 'the benchmark crashed',
            resumable: true,
            resume_from_round: 2,
          }
        : null,
    startedAt,
    reason: null,
  };
}

function randomListing(random: () => number): HostListing {
  const host = pick(random, HOSTS);
  if (random() < 0.1)
    return {kind: 'unavailable', host, connection: {kind: 'offline', detail: 'no route'}};
  const runs = Array.from({length: Math.floor(random() * 6)}, (_, index) =>
    server(index + 1, pick(random, RUN_IDS), pick(random, PHASES)),
  );
  const live = runs.filter(run => ['running', 'paused', 'starting'].includes(run.phase));
  const ended = runs.filter(run => !live.includes(run));
  return {kind: 'listed', host, live, ended, unreadable: [], unverified: []};
}

function randomRecent(random: () => number): RecentRun[] {
  return Array.from({length: Math.floor(random() * 26)}, (_, index) => ({
    host: pick(random, HOSTS),
    hostLabel: 'host',
    project: '~/projects/queue-rs',
    task: random() < 0.5 ? 'mpmc' : null,
    runId: pick(random, RUN_IDS),
    instanceId: null,
    attachedAt: index,
  }));
}

function randomRun(random: () => number): RunRef {
  return {host: pick(random, HOSTS), runId: pick(random, RUN_IDS)};
}

/** A model action: a push, a selection, or a machines reply with any token issued so far. */
function modelAction(random: Seeded, tokens: readonly number[]): SidebarAction {
  const roll = random.next();
  if (roll < 0.15) {
    const hosts = HOSTS.filter(() => random.bool());
    return {
      type: 'machines-listed',
      token: random.pick(tokens),
      hosts: random.bool() ? hosts : null,
    };
  }
  if (roll < 0.35) {
    return {
      type: 'machine-checked',
      token: random.pick(tokens),
      host: random.pick(HOSTS),
      ready: random.bool(),
    };
  }
  return randomAction(() => random.next());
}

function randomAction(random: () => number): SidebarAction {
  const roll = random();
  if (roll < 0.35) return {type: 'listing', listing: randomListing(random)};
  if (roll < 0.5) return {type: 'recent', recent: randomRecent(random)};
  if (roll < 0.65) {
    return {type: 'archived', archived: Array.from({length: 4}, () => randomRun(random))};
  }
  if (roll < 0.75) {
    return {
      type: 'overview',
      listings: Array.from({length: 3}, () => randomListing(random)),
      recent: randomRecent(random),
    };
  }
  const screen = pick(random, ['new-run', 'machines', 'settings', 'all-runs', 'run'] as const);
  return {
    type: 'select',
    screen: screen === 'run' ? {kind: 'run', run: randomRun(random)} : {kind: screen},
  };
}

describe('sidebarStep', () => {
  test('model: holds its invariants and drops stale machine replies over seeded sequences', () => {
    checkModel<SidebarState, SidebarAction, SidebarRequest>({
      initial: initialSidebar().state,
      step: sidebarStep,
      action: (random, tokens) => modelAction(random, tokens),
      issues: request => request.token,
      awaited: state => ({
        listing: state.machines.listing,
        ...Object.fromEntries(
          state.machines.saved.map(machine => [`check:${machine.host}`, machine.checking]),
        ),
      }),
      reply: action =>
        action.type === 'machines-listed'
          ? {slot: 'listing', token: action.token}
          : action.type === 'machine-checked'
            ? {slot: `check:${action.host}`, token: action.token}
            : null,
      invariants: sidebarInvariants,
      also: (before, action, after, requests) => {
        // Leaving the Machines screen lists the saved machines again.
        const left =
          action.type === 'select' &&
          before.selection.kind === 'machines' &&
          action.screen.kind !== 'machines';
        expect(requests.some(request => request.kind === 'list-machines')).toBe(left);
        expect(machineCounts(after).count).toBe(after.machines.saved.length);
      },
    });
  });

  test('regression: the Machines count is the saved machines, with no host watched', () => {
    let step = initialSidebar();
    const listing = step.requests.find(request => request.kind === 'list-machines');
    step = sidebarStep(step.state, {
      type: 'machines-listed',
      token: listing?.token ?? 0,
      hosts: ['local', 'ssh:chelan3', 'ssh:chelan4'],
    });
    expect(step.state.listings).toEqual([]);
    expect(machineCounts(step.state)).toEqual({count: 3, attention: 0});
    let state = step.state;
    for (const request of step.requests) {
      if (request.kind !== 'check-machine') continue;
      state = sidebarStep(state, {
        type: 'machine-checked',
        token: request.token,
        host: request.host,
        ready: request.host === 'local',
      }).state;
    }
    expect(machineCounts(state)).toEqual({count: 3, attention: 2});
  });

  test('Recent keeps the newest runs up to its limit, without archived or live ones', () => {
    const recent: RecentRun[] = RUN_IDS.map((runId, index) => ({
      host: 'local',
      hostLabel: 'This Mac',
      project: '~/projects/queue-rs',
      task: 'mpmc',
      runId,
      instanceId: null,
      attachedAt: index,
    }));
    let state = sidebarStep(SIDEBAR_EMPTY, {type: 'recent', recent}).state;
    state = sidebarStep(state, {
      type: 'archived',
      archived: [{host: 'local', runId: RUN_IDS[29] as string}],
    }).state;
    state = sidebarStep(state, {
      type: 'listing',
      listing: {
        kind: 'listed',
        host: 'local',
        live: [server(1, RUN_IDS[28] as string, 'paused')],
        ended: [server(2, RUN_IDS[27] as string, 'failed')],
        unreadable: [],
        unverified: [],
      },
    }).state;
    const {rows, more} = recentRows(state);
    expect(rows).toHaveLength(RECENT_ROWS);
    expect(more).toBe(RUN_IDS.length - 2 - RECENT_ROWS);
    expect(rows[0]?.run.runId).toBe(RUN_IDS[27]);
    expect(rows[0]?.status).toBe('failed');
    expect(rows[0]?.title).toBe('queue-rs · mpmc');
    expect(liveRows(state).map(row => [row.run.runId, row.status])).toEqual([
      [RUN_IDS[28] as string, 'paused'],
    ]);
  });
});

describe('row statuses', () => {
  test('read the end record when there is one, and the phase when there is not', () => {
    const ended = (outcome: string | null, phase: HostRun['phase']): HostRun => ({
      ...server(1, 'r', phase),
      end:
        outcome === null
          ? null
          : ({outcome, reason: 'why', resumable: false} as NonNullable<HostRun['end']>),
    });
    const cases: readonly [string | null, HostRun['phase'], RunRowStatus][] = [
      ['adopted', 'completed', 'finished'],
      ['no-winner', 'completed', 'finished'],
      ['deadline', 'stopped', 'finished'],
      ['adoption-failed', 'failed', 'failed'],
      ['force_stopped', 'stopped', 'stopped'],
      [null, 'completed', 'finished'],
      [null, 'failed', 'failed'],
      [null, 'stopped', 'stopped'],
    ];
    for (const [outcome, phase, status] of cases) {
      const state = sidebarStep(
        sidebarStep(SIDEBAR_EMPTY, {
          type: 'recent',
          recent: [
            {
              host: 'local',
              hostLabel: '',
              project: '/p',
              task: null,
              runId: 'r',
              instanceId: null,
              attachedAt: 1,
            },
          ],
        }).state,
        {
          type: 'listing',
          listing: {
            kind: 'listed',
            host: 'local',
            live: [],
            ended: [ended(outcome, phase)],
            unreadable: [],
            unverified: [],
          },
        },
      ).state;
      expect(recentRows(state).rows[0]?.status, `${outcome} ${phase}`).toBe(status);
    }
  });

  test('a paused run says who paused it', () => {
    const state = sidebarStep(SIDEBAR_EMPTY, {
      type: 'listing',
      listing: {
        kind: 'listed',
        host: 'local',
        live: [server(1, 'r', 'paused')],
        ended: [],
        unreadable: [],
        unverified: [],
      },
    }).state;
    expect(liveRows(state)[0]).toMatchObject({
      status: 'paused',
      detail: 'This Mac · paused: usage limit',
    });
  });
});

describe('runNotices', () => {
  const listed = (live: HostRun[], ended: HostRun[]): HostListing => ({
    kind: 'listed',
    host: 'ssh:chelan4',
    live,
    ended,
    unreadable: [],
    unverified: [],
  });

  test('notifies when a watched run finishes, fails or pauses, never for a first listing', () => {
    const running = [
      server(1, 'a', 'running'),
      server(2, 'b', 'running'),
      server(3, 'c', 'running'),
    ];
    const before = listed(running, []);
    const after = listed(
      [server(3, 'c', 'paused')],
      [server(1, 'a', 'completed'), server(2, 'b', 'failed')],
    );
    expect(runNotices(undefined, after)).toEqual([]);
    expect(runNotices(before, after).map(notice => [notice.kind, notice.run.runId])).toEqual([
      ['finished', 'a'],
      ['failed', 'b'],
      ['paused', 'c'],
    ]);
    expect(runNotices(before, after)[1]?.body).toBe('queue-rs on chelan4: the benchmark crashed');
    expect(runNotices(after, after)).toEqual([]);
  });

  test('notifies each run at most once over seeded sequences of listings', () => {
    for (let seed = 1; seed <= 100; seed++) {
      const random = seeded(seed);
      let before: HostListing | undefined;
      const seen = new Set<string>();
      for (let step = 0; step < 20; step++) {
        const after = randomListing(random);
        for (const notice of runNotices(before, after)) {
          const key = `${notice.kind} ${notice.run.host} ${notice.run.runId} ${step}`;
          expect(seen.has(key)).toBe(false);
          seen.add(key);
          expect(after.host).toBe(notice.run.host);
        }
        before = after;
      }
    }
  });
});
