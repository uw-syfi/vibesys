import {describe, expect, test} from 'bun:test';
import {SYNTHESIZED_TASK_NAME} from '@vibesys/contracts';
import type {DashboardRow} from '@vibesys/feature-app-shell/state';
import {type GroupBy, groupTiles, tilesOf} from '@vibesys/feature-runs-dashboard/state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {runCounts, runInput} from './shell-runs.js';

const HOSTS = ['local', 'ssh:host-a', 'ssh:host-b', 'ssh:host-c'] as const;

test('each machine is counted its own runs, and the counts add up to every run listed', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const rows = Array.from({length: random.int(50)}, () => ({host: random.pick(HOSTS)}));
    const counts = runCounts(rows);
    for (const host of HOSTS) {
      expect(counts.get(host) ?? 0).toBe(rows.filter(row => row.host === host).length);
    }
    expect([...counts.values()].reduce((total, each) => total + each, 0)).toBe(rows.length);
    expect([...counts.values()].every(each => each > 0)).toBe(true);
  }
});

test('every listed run has a tile in its grouping, however many, and the machines count them all', () => {
  const GROUPS: readonly GroupBy[] = ['status', 'machine', 'task', 'agent'];
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
    const grouped = groupTiles(tiles, random.pick(GROUPS)).flatMap(group =>
      group.tiles.map(tile => tile.hint),
    );
    expect(grouped.sort()).toEqual(rows.map(row => row.row.run.runId).sort());
    expect([...runCounts(rows).values()].reduce((total, each) => total + each, 0)).toBe(
      rows.length,
    );
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
    task: 'mpmc',
    project: '/p/default',
    createdAt: Date.parse('2026-10-19T07:11:00Z'),
  } as DashboardRow;
}

describe('the dashboard input of a run', () => {
  const now = Date.parse('2026-10-19T08:00:00Z');

  test('carries why an ended run ended, and the id and task it is named by', () => {
    const input = runInput(entry('failed', 'the agent crashed'), now);
    expect(input.reason).toBe('the agent crashed');
    expect(input.runId).toBe('20261019-071100');
    expect(input.task).toBe('mpmc');
  });

  test('groups a run with no task of its own under its title, never the synthesized task name', () => {
    const input = runInput({...entry('live', null), task: null}, now);
    expect(input.task).toBe('queue-rs');
    expect(input.task).not.toBe(SYNTHESIZED_TASK_NAME);
  });

  test('is titled as its row is, the one title every screen gives the run', () => {
    expect(runInput(entry('live', null), now).title).toBe('queue-rs');
  });

  test('does not read what a live run is doing as a reason', () => {
    expect(runInput(entry('live', 'running · round 2'), now).reason).toBeNull();
  });
});
