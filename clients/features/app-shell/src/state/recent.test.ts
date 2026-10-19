import {describe, expect, test} from 'bun:test';
import type {
  ConnectionStatus,
  HostListing,
  HostRun,
  RunDigestEntry,
  RunDigestsReply,
} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {
  type Clock,
  initialSidebar,
  recentRows,
  SIDEBAR_EMPTY,
  type SidebarRequest,
  type SidebarState,
  sidebarInvariants,
  sidebarStep,
} from './index.js';
import {
  formatDuration,
  formatRatio,
  formatStarted,
  formatWhen,
  resultWords,
} from './recent-words.js';

const NOW = Date.parse('2026-10-17T12:00:00Z');
const CLOCK: Clock = {now: NOW, timeZone: 'UTC'};
const HOUR = 3_600_000;

describe('formatWhen', () => {
  const at = (iso: string): string | null => formatWhen(Date.parse(iso), CLOCK);

  test('is the time today, with Yesterday before it, then the date, then the year', () => {
    expect(at('2026-10-17T07:32:00Z')).toBe('07:32');
    expect(at('2026-10-17T00:05:00Z')).toBe('00:05');
    expect(at('2026-10-16T21:10:00Z')).toBe('Yesterday 21:10');
    expect(at('2026-10-15T09:00:00Z')).toBe('Oct 15');
    const nextYear: Clock = {now: Date.parse('2027-01-10T12:00:00Z'), timeZone: 'UTC'};
    expect(formatWhen(Date.parse('2026-12-31T23:59:00Z'), nextYear)).toBe('Dec 31, 2026');
  });

  test('reads the day in the viewer’s zone, not in UTC', () => {
    const tokyo: Clock = {now: NOW, timeZone: 'Asia/Tokyo'};
    // 18:00 UTC on the 16th is 03:00 on the 17th in Tokyo, where it is already the 17th at noon UTC.
    expect(formatWhen(Date.parse('2026-10-16T18:00:00Z'), tokyo)).toBe('03:00');
  });
});

describe('formatStarted', () => {
  test('is always the day and the time, with the year only when it is not this one', () => {
    const at = (iso: string): string | null => formatStarted(Date.parse(iso), CLOCK);
    expect(at('2026-10-17T07:32:00Z')).toBe('Oct 17 07:32');
    expect(at('2026-10-19T04:21:00Z')).toBe('Oct 19 04:21');
    const nextYear: Clock = {now: Date.parse('2027-01-10T12:00:00Z'), timeZone: 'UTC'};
    expect(formatStarted(Date.parse('2026-12-31T23:59:00Z'), nextYear)).toBe('Dec 31, 2026 23:59');
    expect(formatStarted(0, CLOCK)).toBeNull();
    expect(formatStarted(Number.NaN, CLOCK)).toBeNull();
  });
});

describe('when a Recent row says a run began', () => {
  const WORDS = '3.83×';
  /** Moments no run began at: unset, negative, not a number, or an epoch the product predates. */
  const NO_TIME = [
    0,
    -1,
    -86_400_000,
    Number.NaN,
    Number.NEGATIVE_INFINITY,
    Number.POSITIVE_INFINITY,
    3,
    3_000,
    86_400_000,
    Date.UTC(2000, 0, 1),
    Date.UTC(2026, 0, 1),
    Date.parse('2026-05-07T00:00:00Z') - 1,
  ];

  function detailOf(createdAt: number, startedAt: number | null): string | undefined {
    const ended: HostRun[] =
      startedAt === null
        ? []
        : [
            {
              id: 's1',
              projectRoot: '/p/queue-rs',
              runId: 'r1',
              status: 'completed',
              phase: 'completed',
              paused: null,
              end: {outcome: 'adopted', reason: 'adopted the best', resumable: false},
              startedAt,
              reason: null,
            },
          ];
    const first = sidebarStep(SIDEBAR_EMPTY, {
      type: 'listing',
      listing: {...listed(), ended},
    });
    const opened = sidebarStep(first.state, {
      type: 'recent',
      recent: [recent('r1', '/p/queue-rs', 'mpmc', 1)],
    });
    const answered = answer(
      opened.state,
      [...first.requests, ...opened.requests],
      new Map([['r1', read({createdAt})]]),
    );
    return recentRows(answered, CLOCK).rows[0]?.detail;
  }

  test('a time before the product existed is never worded as a date, in any zone', () => {
    for (const bad of NO_TIME) {
      for (const timeZone of ['UTC', 'America/Los_Angeles', 'Pacific/Kiritimati']) {
        expect(formatWhen(bad, {now: NOW, timeZone}), `${bad} in ${timeZone}`).toBeNull();
      }
      for (const startedAt of [null, bad / 1000]) {
        const detail = detailOf(bad, startedAt);
        expect(detail, `${bad} ${startedAt}`).toBe(WORDS);
        expect(detail).not.toMatch(/19(69|70)/);
      }
    }
  });

  test('a moment since the product existed is always worded', () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const at = Date.parse('2026-05-07T00:00:00Z') + ((seed * 104_729) % 5_000_000) * 60_000;
      expect(formatWhen(at, {now: NOW, timeZone: 'UTC'})).not.toBeNull();
    }
  });

  test('a positive time is worded as usual, from the digest first and the server otherwise', () => {
    for (let seed = 1; seed <= 60; seed += 1) {
      const ago = ((seed * 7919) % 5000) * 60_000 + 1;
      const at = NOW - ago;
      expect(detailOf(at, null)).toBe(`${formatWhen(at, CLOCK)} · ${WORDS}`);
      const server = NOW - ago * 2;
      expect(detailOf(Number.NaN, server / 1000)).toBe(`${formatWhen(server, CLOCK)} · ${WORDS}`);
    }
  });
});

describe('numbers of a row', () => {
  test('a duration says its two largest units, and a ratio its unit', () => {
    expect([0, 45, 60, 3120, 3599, 3900, 7384].map(formatDuration)).toEqual([
      '0s',
      '45s',
      '1m',
      '52m',
      '59m',
      '1h 05m',
      '2h 03m',
    ]);
    expect(formatRatio(3.826)).toBe('3.83×');
  });

  test('a success says its ratio; a failure or a stop says why, a mixed ending says both', () => {
    const base = {word: 'finished', ok: true, ratio: 3.83, ideas: 10};
    expect(resultWords({...base, kind: 'finished'})).toBe('3.83×');
    expect(resultWords({...base, kind: 'finished', ratio: null})).toBe('finished');
    expect(resultWords({...base, kind: 'finished', ok: false, word: 'time limit reached'})).toBe(
      'time limit reached · 3.83×',
    );
    expect(resultWords({...base, kind: 'stopped', ok: false, word: 'stopped', ideas: 2})).toBe(
      'stopped · 2 ideas',
    );
    expect(resultWords({...base, kind: 'stopped', ok: false, word: 'stopped', ideas: 0})).toBe(
      'stopped · no ideas',
    );
    expect(resultWords({...base, kind: 'failed', ok: false, word: 'failed', ideas: 0})).toBe(
      'failed to start',
    );
    expect(resultWords({...base, kind: 'failed', ok: false, word: 'failed', ideas: 3})).toBe(
      'failed · 3 ideas',
    );
    expect(resultWords({...base, kind: 'failed', ok: false, word: 'failed', ideas: null})).toBe(
      'failed',
    );
  });
});

const recent = (runId: string, project: string, task: string, order: number): RecentRun => ({
  host: 'ssh:host-a',
  hostLabel: 'host-a',
  project,
  task,
  runId,
  instanceId: null,
  attachedAt: order,
});

const read = (over: Partial<Extract<RunDigestEntry, {kind: 'read'}>> = {}): RunDigestEntry => ({
  kind: 'read',
  createdAt: NOW - 2 * HOUR,
  ideas: 10,
  adoptedRatio: 3.83,
  bestSoFarRatio: null,
  durationSeconds: 52 * 60,
  outcome: 'adopted',
  closing: 'finished',
  over: true,
  ...over,
});

const listed = (): Extract<HostListing, {kind: 'listed'}> => ({
  kind: 'listed',
  host: 'ssh:host-a',
  live: [],
  ended: [],
  unreadable: [],
  unverified: [],
});

/** The state after Recent's runs and an empty registry, and the requests it made. */
function open(runs: readonly RecentRun[]): {
  state: SidebarState;
  requests: readonly SidebarRequest[];
} {
  const first = sidebarStep(SIDEBAR_EMPTY, {type: 'listing', listing: listed()});
  const step = sidebarStep(first.state, {type: 'recent', recent: runs});
  const digests = step.requests.filter(request => request.kind === 'read-digests');
  return {state: step.state, requests: digests};
}

function answer(
  state: SidebarState,
  requests: readonly SidebarRequest[],
  runs: ReadonlyMap<string, RunDigestEntry>,
): SidebarState {
  let next = state;
  for (const request of requests) {
    if (request.kind !== 'read-digests') continue;
    const reply: RunDigestsReply = {
      kind: 'digests',
      runs: new Map(request.runIds.flatMap(id => (runs.has(id) ? [[id, runs.get(id)!]] : []))),
    };
    next = sidebarStep(next, {type: 'digests-read', token: request.token, reply}).state;
  }
  return next;
}

describe('Recent rows from run digests', () => {
  test('regression: a run the registry no longer lists says how it ended, not "status unknown"', () => {
    const {state, requests} = open([recent('r1', '/p/queue-rs', 'mpmc', 1)]);
    expect(requests).toEqual([
      {kind: 'read-digests', token: 1, host: 'ssh:host-a', project: '/p/queue-rs', runIds: ['r1']},
    ]);
    expect(recentRows(state, CLOCK).rows[0]?.detail).toBe('reading…');

    const answered = answer(state, requests, new Map([['r1', read()]]));
    const row = recentRows(answered, CLOCK).rows[0];

    expect(row?.detail).toBe('10:00 · 3.83×');
    expect(row?.trail).toBe('10 ideas · 52m');
    expect(row?.status).toBe('finished');
    expect(row?.problem).toBeNull();
    expect(row?.tooltip).toContain('host-a · run r1');
    expect(sidebarInvariants(answered)).toEqual([]);
  });

  test('a stopped run says why instead of a ratio, and a run with no ideas failed to start', () => {
    const {state, requests} = open([
      recent('r1', '/p/queue-rs', 'mpmc', 2),
      recent('r2', '/p/queue-rs', 'mpmc', 1),
    ]);
    const answered = answer(
      state,
      requests,
      new Map([
        ['r1', read({outcome: 'stopped', closing: 'interrupted', ideas: 2})],
        ['r2', read({outcome: 'failed', closing: 'failed', ideas: 0, adoptedRatio: null})],
      ]),
    );
    const [first, second] = recentRows(answered, CLOCK).rows;
    expect(first?.detail).toBe('10:00 · stopped · 2 ideas');
    expect(first?.trail).toBe('52m');
    expect(second?.detail).toBe('10:00 · failed to start');
  });

  test('a finished run recorded before the run trace says its ideas are not recorded, not zero', () => {
    const {state, requests} = open([recent('r1', '/p/queue-rs', 'mpmc', 1)]);

    const answered = answer(
      state,
      requests,
      new Map([
        ['r1', read({ideas: null, adoptedRatio: null, outcome: null, closing: 'finished'})],
      ]),
    );
    const row = recentRows(answered, CLOCK).rows[0];

    expect(row?.status).toBe('finished');
    expect(row?.trail).toBe('ideas not recorded · 52m');
    expect(JSON.stringify(row)).not.toContain('0 ideas');
  });

  test('a run whose journal never says it ended reads as stopped unexpectedly', () => {
    const {state, requests} = open([recent('r1', '/p/queue-rs', 'mpmc', 1)]);
    const answered = answer(
      state,
      requests,
      new Map([['r1', read({outcome: null, closing: null, over: false})]]),
    );
    expect(recentRows(answered, CLOCK).rows[0]?.look.word).toBe('stopped unexpectedly');
  });

  test('a run that cannot be read says so with a retry, and one the host has no record of is gone', () => {
    const {state, requests} = open([
      recent('r1', '/p/queue-rs', 'mpmc', 2),
      recent('r2', '/p/queue-rs', 'mpmc', 1),
    ]);
    const answered = answer(
      state,
      requests,
      new Map([['r1', {kind: 'unreadable', createdAt: NOW - HOUR, message: 'x'}]]),
    );
    const [unreadable, missing] = recentRows(answered, CLOCK).rows;
    expect(unreadable?.problem).toEqual({text: "can't read run state", retry: true});
    expect(missing?.problem).toEqual({text: 'this run is gone', retry: false});
    expect(unreadable?.detail).toBe("11:00 · can't read run state");
    for (const row of [unreadable, missing]) expect(row?.detail).not.toContain('status unknown');

    const retried = sidebarStep(answered, {
      type: 'retry-digest',
      run: {host: 'ssh:host-a', runId: 'r1'},
    });
    expect(retried.requests).toEqual([
      {kind: 'read-digests', token: 2, host: 'ssh:host-a', project: '/p/queue-rs', runIds: ['r1']},
    ]);
    expect(recentRows(retried.state, CLOCK).rows[0]?.problem).toBeNull();
  });

  test('a host that offers no digests says its VibeSys is too old, without a retry', () => {
    const {state, requests} = open([recent('r1', '/p/queue-rs', 'mpmc', 1)]);
    const [request] = requests;
    const answered = sidebarStep(state, {
      type: 'digests-read',
      token: request?.kind === 'read-digests' ? request.token : 0,
      reply: {kind: 'unsupported'},
    }).state;
    expect(recentRows(answered, CLOCK).rows[0]?.problem).toEqual({
      text: 'needs an update',
      retry: false,
    });
  });

  test('a failed ask on a machine that is not listing words that machine, an old one as needing an update', () => {
    const cases: readonly (readonly [ConnectionStatus, string, boolean])[] = [
      [{kind: 'offline', detail: 'down'}, 'unreachable', true],
      [{kind: 'closed'}, 'unreachable', true],
      [{kind: 'auth-needed', detail: 'who'}, 'sign in', true],
      [{kind: 'incompatible', detail: 'old'}, 'needs an update', false],
    ];
    for (const [connection, text, retry] of cases) {
      const {state} = open([recent('r1', '/p/queue-rs', 'mpmc', 1)]);
      const down = sidebarStep(state, {
        type: 'listing',
        listing: {kind: 'unavailable', host: 'ssh:host-a', connection},
      }).state;
      const failed = sidebarStep(down, {type: 'digests-read', token: 1, reply: null}).state;
      expect(recentRows(failed, CLOCK).rows[0]?.problem).toEqual({text, retry});
    }
  });

  test('a failed ask is a problem that can be retried, and a late reply is dropped', () => {
    const {state} = open([recent('r1', '/p/queue-rs', 'mpmc', 1)]);
    const failed = sidebarStep(state, {type: 'digests-read', token: 1, reply: null}).state;
    expect(recentRows(failed, CLOCK).rows[0]?.problem?.retry).toBe(true);
    const stale = sidebarStep(failed, {
      type: 'digests-read',
      token: 99,
      reply: {kind: 'digests', runs: new Map([['r1', read()]])},
    }).state;
    expect(recentRows(stale, CLOCK).rows[0]?.problem?.retry).toBe(true);
  });

  test('a run that is live again forgets its digest, and is read afresh when it ends', () => {
    const {state, requests} = open([recent('r1', '/p/queue-rs', 'mpmc', 1)]);
    const answered = answer(state, requests, new Map([['r1', read()]]));
    const live: HostListing = {
      ...listed(),
      live: [
        {
          id: '000000000001',
          projectRoot: '/p/queue-rs',
          runId: 'r1',
          status: 'running',
          phase: 'running' as const,
          paused: null,
          end: null,
          startedAt: 1,
          reason: null,
        },
      ],
    };
    const resumed = sidebarStep(answered, {type: 'listing', listing: live}).state;
    expect(resumed.digests).toEqual({});
    const ended = sidebarStep(resumed, {type: 'listing', listing: listed()});
    expect(ended.requests).toHaveLength(1);
  });

  test('one request per project on a host, naming only the runs that show', () => {
    const {requests} = open([
      recent('a', '/p/queue-rs', 'mpmc', 4),
      recent('b', '/p/kv-store', 'get-put', 3),
      recent('c', '/p/queue-rs', 'mpmc', 2),
    ]);
    expect(
      requests.map(each => (each.kind === 'read-digests' ? [each.project, each.runIds] : null)),
    ).toEqual([
      ['/p/queue-rs', ['a', 'c']],
      ['/p/kv-store', ['b']],
    ]);
  });
});

describe('the first start and Settings', () => {
  test("the first start asks for nothing: the saved machines are the Machines store's", () => {
    expect(initialSidebar().requests).toEqual([]);
    expect(initialSidebar().state.selection).toEqual({kind: 'runs'});
  });

  test('Settings back restores the exact previous screen after repeated opens', () => {
    const previous = {kind: 'run', run: {host: 'ssh:host-a', runId: 'r1'}} as const;
    let state = sidebarStep(SIDEBAR_EMPTY, {type: 'select', screen: previous}).state;
    state = sidebarStep(state, {type: 'select', screen: {kind: 'settings'}}).state;
    state = sidebarStep(state, {type: 'select', screen: {kind: 'settings'}}).state;
    state = sidebarStep(state, {type: 'back-from-settings'}).state;
    expect(state.selection).toEqual(previous);
  });

  test('Settings opened from Machines or Usage goes back to that page, not to the one before it', () => {
    for (const from of [{kind: 'machines'}, {kind: 'usage'}] as const) {
      let state = sidebarStep(SIDEBAR_EMPTY, {
        type: 'select',
        screen: {kind: 'new-run', machine: 'ssh:host-b'},
      }).state;
      state = sidebarStep(state, {type: 'select', screen: from}).state;
      state = sidebarStep(state, {type: 'select', screen: {kind: 'settings'}}).state;
      state = sidebarStep(state, {type: 'back-from-settings'}).state;
      expect(state.selection).toEqual(from);
    }
  });
});
