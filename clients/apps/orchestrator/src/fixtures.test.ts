import {describe, expect, test} from 'bun:test';
import {FIXTURE_NAMES, fixtureHosts} from './fixtures.js';

describe('the fixture hosts', () => {
  test('each fixture opens with a snapshot of a run, through the ports a daemon would give', async () => {
    for (const name of FIXTURE_NAMES.filter(known => known !== 'disconnected')) {
      for (const host of fixtureHosts(name, null)) {
        const watch = host.ports.openRuns(0n);
        const first = await watch.frames[Symbol.asyncIterator]().next();
        watch.close();
        expect(first.value?.case).toBe('snapshot');
        expect(Object.keys(first.value?.value?.runs ?? {}).length).toBeGreaterThan(0);
      }
    }
  });

  test('the disconnected fixture has a host that answers and one that cannot be reached', async () => {
    const [up, down] = fixtureHosts('disconnected', null);
    const first = await up?.ports.openRuns(0n).frames[Symbol.asyncIterator]().next();
    expect(first?.value?.case).toBe('snapshot');
    await expect(down?.ports.openRuns(0n).frames[Symbol.asyncIterator]().next()).rejects.toThrow(
      'no route to host',
    );
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
