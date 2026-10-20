import {expect, test} from 'bun:test';
import type {HostListing, HostRun, ListedRun} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {dashboardRows, SIDEBAR_EMPTY, sidebarInvariants, sidebarStep} from './index.js';

const NOW = Date.parse('2026-10-17T12:00:00Z');
const HOST = 'ssh:host-a';
const PROJECT = '/home/me/p';

const server = (id: string, phase: 'running' | 'failed', startedAt: number): HostRun => ({
  id,
  projectRoot: PROJECT,
  runId: 'r1',
  status: phase,
  phase,
  paused: null,
  end: phase === 'running' ? null : {outcome: 'failed', reason: 'exited', resumable: false},
  startedAt,
  reason: null,
});

const remembered = (project: string): RecentRun => ({
  host: HOST,
  hostLabel: 'host-a',
  project,
  task: 'mpmc',
  runId: 'r1',
  instanceId: null,
  attachedAt: 1,
});

const listing = (live: HostRun[], ended: HostRun[]): HostListing => ({
  kind: 'listed',
  host: HOST,
  live,
  ended,
  unreadable: [],
  unverified: [],
});

/** The dashboard's rows once the machine answered every project the app asked it about. */
function rowsOf(world: HostListing, recent: readonly RecentRun[]) {
  const first = sidebarStep(SIDEBAR_EMPTY, {
    type: 'overview',
    listings: [world],
    recent,
    skippedRecent: [],
  });
  const run: ListedRun = {runId: 'r1', name: 'r1', task: 'mpmc', createdAt: NOW};
  let state = first.state;
  for (const request of first.requests) {
    if (request.kind !== 'list-project-runs') continue;
    // The host resolves every spelling of the directory to the one project, and lists its run.
    state = sidebarStep(state, {
      type: 'project-runs-listed',
      token: request.token,
      host: request.host,
      project: request.project,
      runs: [run],
    }).state;
  }
  expect(sidebarInvariants(state)).toEqual([]);
  return dashboardRows(state, {now: NOW, timeZone: 'UTC'});
}

const ENDED = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'].map((letter, at) =>
  server(`00000000000${letter}`, 'failed', 100 + at),
);

test('a run resumed many times is one row, running while one server drives it', () => {
  const world = listing([server('0000000000ff', 'running', 200)], ENDED);
  for (const spellings of [[PROJECT], [PROJECT, '~/p'], ['~/p', `${PROJECT}/`, PROJECT]]) {
    const rows = rowsOf(world, spellings.map(remembered));
    expect(rows.map(each => [each.row.run.runId, each.row.status])).toEqual([['r1', 'live']]);
  }
});

test('a run resumed many times is one row, as the latest ending says when none is live', () => {
  const rows = rowsOf(listing([], ENDED), [remembered(PROJECT), remembered('~/p')]);
  expect(rows.map(each => [each.row.run.runId, each.row.status])).toEqual([['r1', 'failed']]);
});
