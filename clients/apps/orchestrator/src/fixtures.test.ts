import {describe, expect, test} from 'bun:test';
import {FIXTURE_NAMES, fixtureHosts} from './fixtures.js';

describe('the fixture hosts', () => {
  test('each fixture opens with a snapshot of a run, through the ports a daemon would give', async () => {
    for (const name of FIXTURE_NAMES) {
      for (const host of fixtureHosts(name, null)) {
        const watch = host.ports.openRuns(0n);
        const first = await watch.frames[Symbol.asyncIterator]().next();
        watch.close();
        expect(first.value?.case).toBe('snapshot');
        expect(Object.keys(first.value?.value?.runs ?? {}).length).toBeGreaterThan(0);
      }
    }
  });

  test('a run revealed event by event ends where the whole one starts', () => {
    const [host] = fixtureHosts('short-run', 1);
    if (host === undefined) throw new Error('the fixture has a host');
    let steps = 0;
    while (host.advance()) steps += 1;
    expect(steps).toBeGreaterThan(10);
    expect(host.advance()).toBe(false);
  });
});
