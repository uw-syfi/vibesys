import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
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

  test('a synchronous release failure cannot skip both the bound and process exit', () => {
    const exits: number[] = [];
    let bounds = 0;
    const app = new AppLifecycle({
      release: () => {
        throw new Error('release failed before returning its promise');
      },
      exit: code => exits.push(code),
      scheduleTimeout: () => {
        bounds += 1;
        return () => {};
      },
      releaseBoundMs: 5_000,
    });

    expect(() => app.quit(7)).not.toThrow();
    expect(app.state).toEqual({kind: 'exited', code: 7});
    expect(exits).toEqual([7]);
    expect(bounds).toBe(0);
  });

  test('a bound acquisition failure exits instead of waiting forever for release', () => {
    const exits: number[] = [];
    const app = new AppLifecycle({
      release: () => new Promise<void>(() => {}),
      exit: code => exits.push(code),
      scheduleTimeout: () => {
        throw new Error('timer unavailable');
      },
      releaseBoundMs: 5_000,
    });

    expect(() => app.quit(4)).not.toThrow();
    expect(app.state).toEqual({kind: 'exited', code: 4});
    expect(exits).toEqual([4]);
  });

  test('a failed bound cancellation cannot prevent process exit', async () => {
    const exits: number[] = [];
    const app = new AppLifecycle({
      release: () => Promise.resolve(),
      exit: code => exits.push(code),
      scheduleTimeout: () => () => {
        throw new Error('timer cancellation failed');
      },
      releaseBoundMs: 5_000,
    });

    app.quit(3);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(app.state).toEqual({kind: 'exited', code: 3});
    expect(exits).toEqual([3]);
  });

  test('a reentrant bound is retired after its callback exits the app', () => {
    const exits: number[] = [];
    let cancellations = 0;
    const app = new AppLifecycle({
      release: () => new Promise<void>(() => {}),
      exit: code => exits.push(code),
      scheduleTimeout: callback => {
        callback();
        return () => {
          cancellations += 1;
        };
      },
      releaseBoundMs: 0,
    });

    app.quit(2);

    expect(app.state).toEqual({kind: 'exited', code: 2});
    expect(exits).toEqual([2]);
    expect(cancellations).toBe(1);
  });

  test('a reentrant bound retires its handle without swallowing an exit failure', () => {
    const failure = new Error('exit failed');
    let cancellations = 0;
    const app = new AppLifecycle({
      release: () => new Promise<void>(() => {}),
      exit: () => {
        throw failure;
      },
      scheduleTimeout: callback => {
        callback();
        return () => {
          cancellations += 1;
        };
      },
      releaseBoundMs: 0,
    });

    expect(() => app.quit(6)).toThrow(failure);
    expect(app.state).toEqual({kind: 'exited', code: 6});
    expect(cancellations).toBe(1);
  });

  test('a callback failure wins when a reentrant scheduler also fails', () => {
    const exitFailure = new Error('exit failed');
    const app = new AppLifecycle({
      release: () => new Promise<void>(() => {}),
      exit: () => {
        throw exitFailure;
      },
      scheduleTimeout: callback => {
        callback();
        throw new Error('scheduler failed after callback');
      },
      releaseBoundMs: 0,
    });

    expect(() => app.quit(8)).toThrow(exitFailure);
    expect(app.state).toEqual({kind: 'exited', code: 8});
  });

  test('a deferred bound does not swallow an exit failure', () => {
    const failure = new Error('exit failed after the bound');
    const clock = new FakeClock();
    const app = new AppLifecycle({
      release: () => new Promise<void>(() => {}),
      exit: () => {
        throw failure;
      },
      scheduleTimeout: clock.scheduleTimeout,
      releaseBoundMs: 1,
    });

    app.quit(9);

    expect(() => clock.advanceBy(1)).toThrow(failure);
    expect(app.state).toEqual({kind: 'exited', code: 9});
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
