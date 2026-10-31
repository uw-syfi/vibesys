import {describe, expect, test} from 'bun:test';
import type {EndTone, HostKey, HostListing, HostRun} from '@vibesys/hosts';
import type {RecentRun, RunRef} from '@vibesys/runs';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  initialSidebar,
  liveRows,
  RECENT_ROWS,
  type RunRowStatus,
  recentRows,
  runNotices,
  runProjectRoot,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  sidebarInvariants,
  sidebarStep,
} from './index.js';

const HOSTS: readonly HostKey[] = ['local', 'ssh:host-b', 'ssh:gpu'];
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
    return {
      kind: 'unavailable',
      host,
      step: 'gateway',
      connection: {kind: 'offline', detail: 'no route'},
    };
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

/** A model action: a push or a selection. */
function modelAction(random: Seeded): SidebarAction {
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
      skippedRecent:
        random() < 0.5 ? [] : [{index: 1, reason: 'recent.json: runs[1].task must be a task name'}],
    };
  }
  const screen = pick(random, ['new-run', 'machines', 'settings', 'runs', 'usage', 'run'] as const);
  return {
    type: 'select',
    screen:
      screen === 'run'
        ? {kind: 'run', run: randomRun(random)}
        : screen === 'new-run' && random() < 0.5
          ? {kind: 'new-run', machine: pick(random, ['local', 'ssh:host-b'])}
          : {kind: screen},
  };
}

describe('sidebarStep', () => {
  test('model: holds its invariants over seeded sequences', () => {
    checkModel<SidebarState, SidebarAction, SidebarRequest>({
      initial: initialSidebar().state,
      step: sidebarStep,
      action: random => modelAction(random),
      issues: request => (request.kind === 'push-address' ? null : request.token),
      awaited: () => ({}),
      reply: () => null,
      invariants: sidebarInvariants,
    });
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

  test('regression: only a run that adopted or kept a result gets the green dot', () => {
    const tones: readonly [string, EndTone, string][] = [
      ['adopted', 'ok', 'finished'],
      ['kept-input', 'ok', 'finished · kept the input'],
      ['no-winner', 'neutral', 'no winner'],
      ['deadline', 'warn', 'time limit reached'],
      ['finish-refused', 'warn', 'not adopted'],
      ['failed', 'err', 'failed'],
      ['stopped', 'neutral', 'stopped'],
    ];
    for (const [outcome, tone, word] of tones) {
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
            ended: [
              {
                ...server(1, 'r', 'completed'),
                end: {outcome, reason: 'why', resumable: false} as NonNullable<HostRun['end']>,
              },
            ],
            unreadable: [],
            unverified: [],
          },
        },
      ).state;
      const row = recentRows(state).rows[0];
      expect(row?.look, outcome).toEqual({tone, word});
      expect(row?.detail, outcome).toBe(word);
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
    host: 'ssh:host-b',
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
    expect(runNotices(before, after)[1]?.body).toBe('queue-rs on host-b: the benchmark crashed');
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

test('property: unresolved live runs keep their sidebar state and notices without a control address', () => {
  for (let seed = 0; seed < 64; seed++) {
    const phase = seed % 2 === 0 ? 'running' : 'paused';
    const unresolved = (status: 'running' | 'paused'): HostListing => {
      const {projectRoot: _, ...run} = server(seed, `unknown-${seed}`, status);
      return {
        kind: 'listed',
        host: 'ssh:host-b',
        live: [],
        ended: [],
        unreadable: [],
        unverified: [],
        unknownProject: [{...run, alive: true}],
      };
    };
    const before = unresolved('running');
    const after = unresolved(phase);
    const step = sidebarStep(SIDEBAR_EMPTY, {type: 'listing', listing: after});
    const state = step.state;
    expect(step.requests.filter(request => request.kind === 'read-digests')).toEqual([]);
    expect(liveRows(state)[0]).toMatchObject({
      title: 'Unknown project',
      status: phase === 'paused' ? 'paused' : 'live',
    });
    expect(liveRows(state)[0]?.tooltip).not.toContain(`unknown-${seed}`);
    expect(runProjectRoot(state, {host: after.host, runId: `unknown-${seed}`})).toBeUndefined();
    expect(runNotices(before, after).map(notice => notice.kind)).toEqual(
      phase === 'paused' ? ['paused'] : [],
    );
  }
});
