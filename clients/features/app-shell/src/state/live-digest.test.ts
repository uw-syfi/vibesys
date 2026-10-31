import {describe, expect, test} from 'bun:test';
import type {HostListing, HostRun, RunDigestEntry} from '@vibesys/hosts';
import {
  initialSidebar,
  liveRows,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  sidebarStep,
} from './index.js';

const HOST = 'ssh:host-b';
const PROJECT = '~/projects/queue-rs';

function liveRun(runId: string): HostRun {
  return {
    id: '00000000000a',
    projectRoot: PROJECT,
    runId,
    status: 'running',
    phase: 'running',
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  };
}

function listing(...runIds: string[]): HostListing {
  return {
    kind: 'listed',
    host: HOST,
    live: runIds.map(liveRun),
    ended: [],
    unreadable: [],
    unverified: [],
  };
}

function digest(over: Partial<Extract<RunDigestEntry, {kind: 'read'}>>): RunDigestEntry {
  return {
    kind: 'read',
    createdAt: 0,
    ideas: 0,
    adoptedRatio: null,
    bestSoFarRatio: null,
    durationSeconds: null,
    outcome: null,
    closing: null,
    over: false,
    ...over,
  };
}

/** The state after `actions`, and every request made along the way. */
function play(...actions: SidebarAction[]): {
  state: SidebarState;
  requests: SidebarRequest[];
} {
  let state = initialSidebar().state;
  const requests: SidebarRequest[] = [];
  for (const action of actions) {
    const step = sidebarStep(state, action);
    state = step.state;
    requests.push(...step.requests.filter(request => request.kind === 'read-digests'));
  }
  return {state, requests};
}

const OVERVIEW = (...runIds: string[]): SidebarAction => ({
  type: 'overview',
  listings: [listing(...runIds)],
  recent: [],
  skippedRecent: [],
});

const answer = (token: number, runId: string, entry: RunDigestEntry): SidebarAction => ({
  type: 'digests-read',
  token,
  reply: {kind: 'digests', runs: new Map([[runId, entry]])},
});

const trailOf = (state: SidebarState): (string | null)[] => liveRows(state).map(row => row.trail);

describe('the digest of a live run', () => {
  test('a live run is asked for once and says what it has done so far', () => {
    const asked = play(OVERVIEW('r1'));
    expect(asked.requests).toEqual([
      {kind: 'read-digests', token: 1, host: HOST, project: PROJECT, runIds: ['r1']},
    ]);
    expect(trailOf(asked.state)).toEqual([null]);

    const read = play(
      OVERVIEW('r1'),
      answer(1, 'r1', digest({ideas: 9, bestSoFarRatio: 2.37, durationSeconds: 4920})),
      {type: 'listing', listing: listing('r1')},
    );
    expect(read.requests).toHaveLength(1);
    expect(trailOf(read.state)).toEqual(['9 ideas · 2.37× best so far · 1h 22m']);
    expect(liveRows(read.state)[0]?.tooltip).toContain('9 ideas · 2.37× best so far · 1h 22m');
  });

  test('property: the trail names the ideas, the best so far or adopted result, and the time', () => {
    const cases = [null, 0, 1, 4].flatMap(ideas =>
      [null, 30].flatMap(durationSeconds =>
        (
          [
            [null, null],
            [null, 1.5],
            [3, 1.5],
          ] as const
        ).map(([adoptedRatio, bestSoFarRatio]) => ({
          ideas,
          durationSeconds,
          adoptedRatio,
          bestSoFarRatio,
        })),
      ),
    );
    for (const figures of cases) {
      const trail = trailOf(play(OVERVIEW('r1'), answer(1, 'r1', digest(figures))).state)[0] ?? '';
      expect(trail).toContain(figures.ideas === null ? 'ideas not recorded' : `${figures.ideas}`);
      expect(trail.includes('3.00× adopted')).toBe(figures.adoptedRatio !== null);
      expect(trail.includes('1.50× best so far')).toBe(
        figures.adoptedRatio === null && figures.bestSoFarRatio !== null,
      );
      expect(trail.includes('30s')).toBe(figures.durationSeconds !== null);
    }
  });

  test('a refresh asks again, the row keeps the last figures meanwhile, and the answer replaces them', () => {
    const first = digest({ideas: 2, bestSoFarRatio: 1.2, durationSeconds: 60});
    const played = play(OVERVIEW('r1'), answer(1, 'r1', first), {type: 'refresh-live-digests'});
    expect(
      played.requests.map(request => request.kind === 'read-digests' && request.token),
    ).toEqual([1, 2]);
    expect(trailOf(played.state)).toEqual(['2 ideas · 1.20× best so far · 1m']);

    const again = sidebarStep(played.state, {type: 'refresh-live-digests'});
    expect(again.requests).toEqual([]);

    const next = sidebarStep(
      played.state,
      answer(2, 'r1', digest({ideas: 3, bestSoFarRatio: 1.4, durationSeconds: 120})),
    ).state;
    expect(trailOf(next)).toEqual(['3 ideas · 1.40× best so far · 2m']);
  });

  test('an answer that fails leaves the last figures, and a run that stops being live is forgotten', () => {
    const entry = digest({ideas: 2, bestSoFarRatio: 1.2});
    const played = play(OVERVIEW('r1'), answer(1, 'r1', entry), {type: 'refresh-live-digests'});
    const failed = sidebarStep(played.state, {type: 'digests-read', token: 2, reply: null}).state;
    expect(trailOf(failed)).toEqual(['2 ideas · 1.20× best so far']);

    const ended = sidebarStep(failed, {
      type: 'overview',
      listings: [listing()],
      recent: [],
      skippedRecent: [],
    });
    expect(ended.state.live).toEqual({});
    const back = sidebarStep(ended.state, {type: 'listing', listing: listing('r1')});
    expect(back.requests).toHaveLength(1);
  });
});
