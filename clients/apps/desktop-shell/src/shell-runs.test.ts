import {expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {runCounts} from './shell-runs.js';

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
