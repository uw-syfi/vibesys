import {describe, expect, test} from 'bun:test';
import type {DashboardRow} from '@vibesys/feature-app-shell/state';
import {GROUP_BYS, groupTiles, tilesOf} from '@vibesys/feature-runs-dashboard/state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {loadingMachines, runInput} from './shell-runs.js';

const HOSTS = ['local', 'ssh:host-a', 'ssh:host-b', 'ssh:host-c'] as const;

test('a listed machine has no block, a loading one its steps with no retry count, and only a failed one a retry', () => {
  const retried: string[] = [];
  const [loading, failed, ...rest] = loadingMachines(
    [
      {kind: 'listed', host: 'ssh:host-c'},
      {kind: 'loading', host: 'local', step: 'handshake', attempt: 2},
      {kind: 'failed', host: 'ssh:host-a', step: 'gateway', reason: 'helper exited (code 1)'},
    ],
    host => retried.push(host),
  );
  expect(rest).toEqual([]);
  expect(loading?.machine).toBe('This Mac');
  expect(loading?.retry).toBeNull();
  expect(loading?.steps.map(step => [step.state, step.position, step.note])).toEqual([
    ['done', '1/3', undefined],
    ['current', '2/3', undefined],
    ['pending', '3/3', undefined],
  ]);
  expect(failed?.steps.map(step => step.state)).toEqual(['failed', 'pending', 'pending']);
  expect(failed?.steps[0]?.note).toBe('helper exited (code 1)');
  failed?.retry?.();
  expect(retried).toEqual(['ssh:host-a']);
});

test('every listed run has a tile in its grouping, however many', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const rows = Array.from({length: random.int(90)}, (_, index) => ({
      ...entry(random.bool() ? 'live' : 'failed', null),
      host: random.pick(HOSTS),
      row: {
        ...entry('failed', null).row,
        run: {host: random.pick(HOSTS), runId: `run-${index}`},
      },
    }));
    const tiles = tilesOf(rows.map(row => runInput(row, 0)));
    const grouped = groupTiles(tiles, random.pick(GROUP_BYS)).flatMap(group =>
      group.tiles.map(tile => tile.hint),
    );
    expect(grouped.sort()).toEqual(rows.map(row => row.row.run.runId).sort());
  }
});

function entry(status: 'live' | 'failed', now: string | null): DashboardRow {
  return {
    row: {
      run: {host: 'local', runId: '20261019-071100'},
      title: 'queue-rs',
      detail: '',
      trail: null,
      tooltip: '',
      status,
      look: {
        tone: status === 'live' ? 'live' : 'err',
        word: status === 'live' ? 'running' : 'failed',
      },
      problem: null,
      now,
      startedAt: null,
      digest: null,
    },
    host: 'local',
    machine: 'this computer',
    project: '/p/default',
    createdAt: Date.parse('2026-10-19T07:11:00Z'),
  } as DashboardRow;
}

describe('the dashboard input of a run', () => {
  const now = Date.parse('2026-10-19T08:00:00Z');

  test('carries why an ended run ended, and the id it is named by', () => {
    const input = runInput(entry('failed', 'the agent crashed'), now);
    expect(input.reason).toBe('the agent crashed');
    expect(input.runId).toBe('20261019-071100');
  });

  test('is titled as its row is, the one title every screen gives the run', () => {
    expect(runInput(entry('live', null), now).title).toBe('queue-rs');
  });

  test('does not read what a live run is doing as a reason', () => {
    expect(runInput(entry('live', 'running · round 2'), now).reason).toBeNull();
  });
});
