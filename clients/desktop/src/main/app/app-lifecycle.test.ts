import {describe, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/backend-client/testing';
import {SEEDS, Seeded} from '@vibesys/core-state/testing';
import {AppLifecycle, type QuitEffect, type QuitEvent, quitStep, RUNNING} from './app-lifecycle.js';

/** An app whose host release the test settles by hand. */
function world(boundMs = 5_000) {
  const exits: number[] = [];
  let releases = 0;
  let settle: (failed: boolean) => void = () => {};
  const clock = new FakeClock();
  const app = new AppLifecycle({
    release: () => {
      releases += 1;
      return new Promise<void>((resolve, reject) => {
        settle = failed => (failed ? reject(new Error('ssh hung up')) : resolve());
      });
    },
    exit: code => exits.push(code),
    scheduleTimeout: clock.scheduleTimeout,
    releaseBoundMs: boundMs,
  });
  let prevented = 0;
  const event = {preventDefault: () => (prevented += 1)};
  return {
    app,
    clock,
    event,
    exits,
    settle: async (failed: boolean) => {
      settle(failed);
      // The release settles through two promise reactions (catch, then); let them run.
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    },
    counts: () => ({releases, prevented}),
  };
}

describe('AppLifecycle', () => {
  test('regression: a quit ends the process once the hosts are released', async () => {
    // The app used to call app.quit() again from inside the cancelled quit, which Electron
    // ignores: the process stayed alive with no window.
    for (const failed of [false, true]) {
      const {app, event, exits, settle, counts} = world();
      app.willQuit(event);
      expect(counts()).toEqual({releases: 1, prevented: 1});
      expect(exits).toEqual([]);
      await settle(failed);
      expect(exits).toEqual([0]);
    }
  });

  test('a second quit request while releasing exits at once, and only once', async () => {
    const {app, event, exits, settle, counts} = world();
    app.quit(1);
    app.willQuit(event);
    expect(exits).toEqual([1]);
    await settle(false);
    app.quit(0);
    expect(exits).toEqual([1]);
    expect(counts().releases).toBe(1);
  });

  test('a release that hangs is cut off by its bound', () => {
    const {app, clock, exits} = world(5_000);
    app.quit(0);
    clock.advanceBy(4_999);
    expect(exits).toEqual([]);
    clock.advanceBy(1);
    expect(exits).toEqual([0]);
    expect(clock.pending).toBe(0);
  });
});

function count(counted: {releases: number; exits: number[]}, effects: readonly QuitEffect[]): void {
  for (const effect of effects) {
    if (effect.kind === 'release') counted.releases += 1;
    if (effect.kind === 'exit') counted.exits.push(effect.code);
  }
}

/** Fold random events, counting releases and exits, and check every step. */
function quitRound(random: Seeded): void {
  let state = RUNNING;
  const counted = {releases: 0, exits: [] as number[]};
  for (let step = 0; step < 12; step += 1) {
    const event = random.pick<QuitEvent>([
      {type: 'quit', code: random.int(3)},
      {type: 'released'},
      {type: 'bound-elapsed'},
    ]);
    const before = state;
    const next = quitStep(state, event);
    state = next.state;
    count(counted, next.effects);
    if (before.kind === 'exited') expect(next).toEqual({state: before, effects: []});
    // Releasing always has a bound running (no orphan wait): entering it starts one.
    if (before.kind !== 'releasing' && state.kind === 'releasing') {
      expect(next.effects).toContainEqual({kind: 'start-bound'});
    }
    expect(counted.releases).toBeLessThanOrEqual(1);
    expect(counted.exits).toEqual(state.kind === 'exited' ? [state.code] : []);
  }
}

describe('quitStep', () => {
  test('model: one release, at most one exit, exited is absorbing, no orphan wait', () => {
    for (const seed of SEEDS) quitRound(new Seeded(seed));
  });
});
