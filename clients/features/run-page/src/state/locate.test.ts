import {expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {runRecord} from './record.js';
import {
  LOCATE_BOUND_MS,
  type RunPageAction,
  type RunPageState,
  runPageInitial,
  runPageInvariants,
  runPageStep,
} from './run-page.js';
import {headerView, unplacedRun} from './views.js';

/**
 * A run the page cannot place in its host's listing never waits without end: the wait has a bound,
 * and what the page says then names why. Regression: an older finished run, opened from the host's
 * own run list (no recent entry, its tombstone expired), showed "Looking for the run" for ever.
 */

const IDENTITY = {host: 'ssh:host-a', runId: 'run-1'} as const;

const RECENT: RecentRun = {
  host: 'ssh:host-a',
  hostLabel: 'host-a',
  project: '/p',
  task: null,
  runId: 'run-1',
  instanceId: '00000000000a',
  attachedAt: 1,
};

const LISTED: Extract<HostListing, {kind: 'listed'}> = {
  kind: 'listed',
  host: 'ssh:host-a',
  live: [],
  ended: [],
  unreadable: [],
  unverified: [],
};

/** Every listing the host can be in, with whether the run has a recent entry. */
const SITUATIONS: readonly {
  readonly name: string;
  readonly listings: readonly HostListing[];
  readonly recent: RecentRun | undefined;
}[] = [
  {name: 'host not listed yet', listings: [], recent: undefined},
  {name: 'host not listed yet, recent', listings: [], recent: RECENT},
  {
    name: 'host still connecting',
    listings: [
      {kind: 'unavailable', host: 'ssh:host-a', step: 'gateway', connection: {kind: 'connecting'}},
    ],
    recent: undefined,
  },
  {name: 'clean listing, no recent', listings: [LISTED], recent: undefined},
  {name: 'clean listing, recent', listings: [LISTED], recent: RECENT},
  {
    name: 'unverified row, no recent',
    listings: [{...LISTED, unverified: ['00000000000b']}],
    recent: undefined,
  },
  {
    name: 'unreadable row, no recent',
    listings: [{...LISTED, unreadable: ['00000000000b']}],
    recent: undefined,
  },
  {
    name: 'unverified row, recent',
    listings: [{...LISTED, unverified: ['00000000000a']}],
    recent: RECENT,
  },
];

function opened(situation: (typeof SITUATIONS)[number]): ReturnType<typeof runPageStep> {
  const first = runPageStep(
    runPageInitial({
      identity: IDENTITY,
      title: {project: '/p', task: null},
      hostLabel: 'host-a',
      unavailable: {},
    }),
    {type: 'locate'},
  );
  const record = runRecord(situation.listings, IDENTITY, situation.recent);
  const second = runPageStep(first.state, {type: 'record', record});
  return {
    state: second.state,
    requests: [...first.requests, ...second.requests],
  };
}

function boundOf(requests: ReturnType<typeof runPageStep>['requests']): number | null {
  const bound = requests.find(request => request.kind === 'arm-bound');
  return bound?.kind === 'arm-bound' && bound.ms === LOCATE_BOUND_MS ? bound.token : null;
}

for (const situation of SITUATIONS) {
  test(`${situation.name}: the page is placed, or its wait ends in words naming the reason`, () => {
    const {state, requests} = opened(situation);
    expect(runPageInvariants(state)).toEqual([]);
    if (state.record.kind !== 'unlisted') {
      // Placed: nothing waits, and a saved run reads its summary.
      expect(state.locate).toEqual({kind: 'idle'});
      expect(unplacedRun(state)).toBeNull();
      return;
    }
    const token = boundOf(requests);
    expect(token).not.toBeNull();
    expect(unplacedRun(state)).toBeNull();
    const gaveUp: RunPageAction = {type: 'bound-elapsed', token: token ?? -1};
    const settled = runPageStep(state, gaveUp).state;
    expect(runPageInvariants(settled)).toEqual([]);
    const words = unplacedRun(settled);
    expect(words).not.toBeNull();
    expect(words?.words).not.toContain('Looking');
    expect(headerView(settled).status.label).toBe(words?.label ?? '');
  });
}

test('a run no recent entry names, with a clean listing, is a saved run read from its summary', () => {
  const {state, requests} = opened(SITUATIONS[3] as (typeof SITUATIONS)[number]);
  expect(state.record).toEqual({kind: 'historical', status: null});
  expect(requests.some(request => request.kind === 'read-summary')).toBe(true);
});

test('a listing that comes after the bound still places the run', () => {
  const {state, requests} = opened(SITUATIONS[5] as (typeof SITUATIONS)[number]);
  const settled: RunPageState = runPageStep(state, {
    type: 'bound-elapsed',
    token: boundOf(requests) ?? -1,
  }).state;
  expect(unplacedRun(settled)).not.toBeNull();
  const record = runRecord([LISTED], IDENTITY, undefined);
  const placed = runPageStep(settled, {type: 'record', record}).state;
  expect(placed.locate).toEqual({kind: 'idle'});
  expect(unplacedRun(placed)).toBeNull();
});

test('a stale bound does not end a wait that was re-armed', () => {
  const {state} = opened(SITUATIONS[0] as (typeof SITUATIONS)[number]);
  expect(runPageStep(state, {type: 'bound-elapsed', token: 9999}).state.locate).toEqual(
    state.locate,
  );
});
