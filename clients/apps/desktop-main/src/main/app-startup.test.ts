import {describe, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {AppLifecycle} from './app-lifecycle.js';
import {AppStartup, type StartupLifecycle} from './app-startup.js';

type FailureStage = 'manager' | 'window' | 'open' | null;

interface FakeManager extends AsyncDisposable {
  readonly name: 'manager';
}

interface FakeDesktop {
  readonly name: 'desktop';
}

interface Deferred<Value> {
  readonly promise: Promise<Value>;
  resolve(value: Value): void;
}

/** A causal operation controlled by the test, without clocks or scheduler ordering. */
function deferred<Value>(): Deferred<Value> {
  let resolve = (_value: Value): void => {};
  const promise = new Promise<Value>(settle => {
    resolve = settle;
  });
  return {promise, resolve};
}

interface StartupWorld {
  readonly startup: AppStartup<string, FakeManager, FakeDesktop>;
  readonly lifecycle: AppLifecycle;
  readonly lifecyclePort: StartupLifecycle;
  readonly clock: FakeClock;
  readonly events: string[];
  readonly exits: number[];
  readonly logs: string[];
  readonly quitCodes: number[];
  readonly counts: () => {readonly releases: number; readonly opens: number};
}

function world(
  failure: FailureStage,
  cleanup: 'resolves' | 'rejects' | 'hangs' = 'resolves',
): StartupWorld {
  const events: string[] = [];
  const exits: number[] = [];
  const logs: string[] = [];
  const quitCodes: number[] = [];
  let releases = 0;
  let opens = 0;
  const manager: FakeManager = {
    name: 'manager',
    async [Symbol.asyncDispose](): Promise<void> {
      releases += 1;
      events.push('release');
      if (cleanup === 'rejects') throw new Error('cleanup failed');
      if (cleanup === 'hangs') await new Promise<void>(() => {});
    },
  };
  const desktop: FakeDesktop = {name: 'desktop'};
  const startup = new AppStartup<string, FakeManager, FakeDesktop>({
    ready: async () => {
      events.push('ready');
    },
    connectionManager: async () => {
      events.push('manager');
      if (failure === 'manager') throw new Error('manager failed');
      return manager;
    },
    openWindow: async received => {
      expect(received).toBe(manager);
      events.push('window');
      if (failure === 'window') throw new Error('window failed');
      return desktop;
    },
    openInitial: async (received, plan) => {
      expect(received).toBe(desktop);
      opens += 1;
      events.push(`open:${plan}`);
      if (failure === 'open') throw new Error('open failed');
    },
    log: message => logs.push(message),
  });
  const clock = new FakeClock();
  const lifecycle = new AppLifecycle({
    release: () => startup.release(),
    exit: code => exits.push(code),
    scheduleTimeout: clock.scheduleTimeout,
    releaseBoundMs: 5_000,
  });
  const lifecyclePort: StartupLifecycle = {
    quit: code => {
      quitCodes.push(code);
      lifecycle.quit(code);
    },
  };
  return {
    startup,
    lifecycle,
    lifecyclePort,
    clock,
    events,
    exits,
    logs,
    quitCodes,
    counts: () => ({releases, opens}),
  };
}

async function settleLifecycle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe('AppStartup', () => {
  test.each([
    ['manager', ['ready', 'manager'], 0, 0],
    ['window', ['ready', 'manager', 'window', 'release'], 1, 0],
    ['open', ['ready', 'manager', 'window', 'open:initial', 'release'], 1, 1],
  ] as const)('a rejected %s stage reports the cause and quits through the lifecycle once', async (failure, expectedEvents, releases, opens) => {
    const w = world(failure);
    await w.startup.start('initial', w.lifecyclePort);
    await settleLifecycle();

    expect(w.logs).toEqual([`${failure} failed`]);
    expect(w.quitCodes).toEqual([1]);
    expect(w.events).toEqual([...expectedEvents]);
    expect(w.counts()).toEqual({releases, opens});
    expect(w.exits).toEqual([1]);
    expect(w.lifecycle.state).toEqual({kind: 'exited', code: 1});
  });

  test('a cleanup rejection cannot replace the primary startup failure', async () => {
    const w = world('window', 'rejects');
    await w.startup.start('initial', w.lifecyclePort);
    await settleLifecycle();

    expect(w.logs).toEqual(['window failed']);
    expect(w.quitCodes).toEqual([1]);
    expect(w.counts().releases).toBe(1);
    expect(w.exits).toEqual([1]);
  });

  test('a hanging cleanup exits at the lifecycle bound', async () => {
    const w = world('open', 'hangs');
    await w.startup.start('initial', w.lifecyclePort);

    expect(w.exits).toEqual([]);
    w.clock.advanceBy(4_999);
    expect(w.exits).toEqual([]);
    w.clock.advanceBy(1);
    expect(w.exits).toEqual([1]);
    expect(w.quitCodes).toEqual([1]);
    expect(w.counts().releases).toBe(1);
  });

  test('successful startup completes without requesting a quit', async () => {
    const w = world(null);
    await w.startup.start('initial', w.lifecyclePort);

    expect(w.events).toEqual(['ready', 'manager', 'window', 'open:initial']);
    expect(w.logs).toEqual([]);
    expect(w.quitCodes).toEqual([]);
    expect(w.exits).toEqual([]);
  });

  test('release during readiness makes shutdown terminal before manager construction', async () => {
    const ready = deferred<void>();
    const events: string[] = [];
    const startup = new AppStartup<string, FakeManager, FakeDesktop>({
      ready: () => ready.promise,
      connectionManager: async () => {
        events.push('manager');
        return manager(events);
      },
      openWindow: async () => {
        events.push('window');
        return {name: 'desktop'};
      },
      openInitial: async () => {
        events.push('open');
      },
      log: message => events.push(`log:${message}`),
    });
    const quits: number[] = [];
    const starting = startup.start('initial', {quit: code => quits.push(code)});

    const releasing = startup.release();
    ready.resolve();
    await Promise.all([starting, releasing]);

    expect(events).toEqual([]);
    expect(quits).toEqual([]);
  });

  test('release joins an admitted manager acquisition and disposes its late result', async () => {
    const acquisitionStarted = deferred<void>();
    const acquired = deferred<FakeManager>();
    const events: string[] = [];
    const startup = new AppStartup<string, FakeManager, FakeDesktop>({
      ready: async () => {},
      connectionManager: () => {
        events.push('manager');
        acquisitionStarted.resolve();
        return acquired.promise;
      },
      openWindow: async () => {
        events.push('window');
        return {name: 'desktop'};
      },
      openInitial: async () => {
        events.push('open');
      },
      log: message => events.push(`log:${message}`),
    });
    const quits: number[] = [];
    const starting = startup.start('initial', {quit: code => quits.push(code)});
    await acquisitionStarted.promise;

    const releasing = startup.release().then(() => {
      events.push('released');
    });
    acquired.resolve(manager(events));
    await Promise.all([starting, releasing]);

    expect(events).toEqual(['manager', 'release', 'released']);
    expect(quits).toEqual([]);
  });

  test('release while window construction is pending stops the initial launch', async () => {
    const windowStarted = deferred<void>();
    const opened = deferred<FakeDesktop>();
    const events: string[] = [];
    const startup = new AppStartup<string, FakeManager, FakeDesktop>({
      ready: async () => {},
      connectionManager: async () => manager(events),
      openWindow: () => {
        events.push('window');
        windowStarted.resolve();
        return opened.promise;
      },
      openInitial: async () => {
        events.push('open');
      },
      log: message => events.push(`log:${message}`),
    });
    const quits: number[] = [];
    const starting = startup.start('initial', {quit: code => quits.push(code)});
    await windowStarted.promise;

    await startup.release();
    opened.resolve({name: 'desktop'});
    await starting;

    expect(events).toEqual(['window', 'release']);
    expect(quits).toEqual([]);
  });
});

function manager(events: string[]): FakeManager {
  return {
    name: 'manager',
    async [Symbol.asyncDispose](): Promise<void> {
      events.push('release');
    },
  };
}
