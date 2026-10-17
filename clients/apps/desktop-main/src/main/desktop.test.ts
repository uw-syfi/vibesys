import {describe, expect, test} from 'bun:test';
import {WELCOME_CHANNELS, type WelcomeEvent, type WelcomeResult} from '@vibesys/ipc';
import {DEFAULT_PREFERENCES, type RecentFile} from '@vibesys/settings';
import {SHELL_URL} from './app-assets.js';
import {Desktop, type DesktopDeps} from './desktop.js';
import {RunWindows} from './run-windows.js';
import {WelcomeFeed} from './welcome-feed.js';
import type {WelcomeDocument, WelcomeInvokeEvent, WelcomeIpc} from './welcome-ipc.js';

class FakeWelcomeIpc implements WelcomeIpc {
  readonly #handlers = new Map<
    string,
    (event: WelcomeInvokeEvent, ...args: unknown[]) => Promise<WelcomeResult<unknown>>
  >();

  handle(
    channel: string,
    listener: (event: WelcomeInvokeEvent, ...args: unknown[]) => Promise<WelcomeResult<unknown>>,
  ): void {
    this.#handlers.set(channel, listener);
  }

  invoke(
    rendererGeneration: string,
    channel: string,
    ...args: unknown[]
  ): Promise<WelcomeResult<unknown>> {
    return this.invokeAs(7, 'app://vibesys/shell.html', rendererGeneration, channel, ...args);
  }

  invokeAs(
    senderId: number,
    senderUrl: string,
    rendererGeneration: string,
    channel: string,
    ...args: unknown[]
  ): Promise<WelcomeResult<unknown>> {
    const handler = this.#handlers.get(channel);
    if (handler === undefined) throw new Error(`no handler for ${channel}`);
    return handler({senderId, senderUrl, rendererGeneration}, ...args);
  }
}

/** A run window the registry made: its surface's document, and what reached it. */
class FakeRunWindow {
  readonly sent: string[] = [];
  focused = 0;
  readonly #destroyed = new Set<() => void>();
  readonly welcome: WelcomeDocument;

  constructor(
    readonly id: number,
    readonly url: string,
    readonly title: string,
    readonly closed: () => void,
  ) {
    this.welcome = {
      currentId: () => id,
      onNewDocument: () => undefined,
      onDestroyed: listener => this.#destroyed.add(listener),
    };
  }

  focus(): void {
    this.focused += 1;
  }

  sendWelcome<T>(channel: string, _event: WelcomeEvent<T>): void {
    this.sent.push(channel);
  }

  /** The user closes the native window. */
  close(): void {
    for (const listener of this.#destroyed) listener();
    this.closed();
  }
}

class FakeWelcomeDocument implements WelcomeDocument {
  readonly #newDocuments = new Set<(retiringGeneration: string | null) => void>();
  readonly #destroyed = new Set<() => void>();

  currentId(): number {
    return 7;
  }

  onNewDocument(listener: (retiringGeneration: string | null) => void): void {
    this.#newDocuments.add(listener);
  }

  onDestroyed(listener: () => void): void {
    this.#destroyed.add(listener);
  }

  navigate(retiringGeneration: string): void {
    for (const listener of this.#newDocuments) listener(retiringGeneration);
  }
}

class Deferred {
  readonly promise: Promise<void>;
  #resolve: () => void = () => undefined;

  constructor() {
    this.promise = new Promise(resolve => {
      this.#resolve = resolve;
    });
  }

  resolve(): void {
    this.#resolve();
  }
}

interface RetirementCleanups {
  readonly cancelCheckoutOwner?: (owner: string) => Promise<void>;
  readonly retireOwner?: (owner: string) => Promise<void>;
}

function desktopWorld(cleanups: RetirementCleanups = {}) {
  const events: string[] = [];
  const logs: string[] = [];
  const checkouts: {readonly path: unknown; readonly change: unknown}[] = [];
  const launches: string[] = [];
  const opened: string[] = [];
  let bound: ReturnType<DesktopDeps['runs']['attached']> = null;
  let releaseCleanup = (): void => {};
  const cleanup = new Promise<void>(resolve => {
    releaseCleanup = resolve;
  });
  const document = new FakeWelcomeDocument();
  const ipc = new FakeWelcomeIpc();
  const runWindowList: FakeRunWindow[] = [];
  const runWindows = new RunWindows({
    shellUrl: SHELL_URL,
    create: (url, title, closed) => {
      const window = new FakeRunWindow(20 + runWindowList.length, url, title, closed);
      runWindowList.push(window);
      return window;
    },
  });
  const runs: DesktopDeps['runs'] = {
    attached: () => bound,
    attach: async (id, instanceId) => {
      launches.push(`attach:${JSON.stringify(id)}:${instanceId}`);
      bound = {
        host: 'ssh:chelan4',
        hostLabel: 'chelan4',
        instanceId,
        runId: '20261015-163047',
        project: '/p',
        connection: {kind: 'connected'},
      };
    },
    start: async (_id, project) => {
      launches.push(`start:${project}`);
      bound = {
        host: 'local',
        hostLabel: 'This Mac',
        instanceId: '0123456789ab',
        runId: 'run-1',
        project,
        connection: {kind: 'connected'},
      };
      return 'started';
    },
    detach: runId => {
      launches.push(`detach:${runId}`);
      bound = null;
    },
    resume: async () => undefined,
    stop: async () => undefined,
  };
  const hosts: DesktopDeps['hosts'] = {
    setCheckout: async (_id, path, change) => {
      checkouts.push({path, change});
      return String(path);
    },
    cancelCheckout: async () => undefined,
    cancelCheckoutOwner: async owner => {
      events.push(`cancel-checkout:${owner}`);
      if (cleanups.cancelCheckoutOwner !== undefined) {
        await cleanups.cancelCheckoutOwner(owner);
        return;
      }
      await cleanup;
    },
    complete: async (_id, directory) => ({
      kind: 'listed',
      directory,
      entries: [],
      truncated: false,
    }),
    harnesses: async () => ({kind: 'unsupported'}),
    readEvents: async () => ({kind: 'unsupported'}),
    logs: async () => ({ok: true, instanceId: '000000000001', streams: []}),
    recent: async () => undefined,
    releaseLogs: () => undefined,
    retireOwner: async owner => {
      events.push(`retire-host:${owner}`);
      if (cleanups.retireOwner !== undefined) {
        await cleanups.retireOwner(owner);
        return;
      }
      await cleanup;
    },
    signIn: async () => undefined,
    tasks: async () => [],
  };
  const recent: RecentFile = {version: 4, runs: []};
  const settings: DesktopDeps['settings'] = {
    onRecentChanged: () => () => undefined,
    recent: async () => recent,
  };
  const window: DesktopDeps['window'] = {
    welcome: document,
    focus: () => undefined,
    releaseSurfaceRun: () => undefined,
    sendWelcome: <T>(_channel: string, _event: WelcomeEvent<T>): void => undefined,
  };
  const desktop = new Desktop({
    window,
    welcome: new WelcomeFeed(),
    hosts,
    machines: {
      machines: async () => [],
      report: async () => ({kind: 'ready', facts: null}),
      inspect: async () => ({state: 'not_found'}),
      remove: async () => [],
    },
    runs,
    settings,
    preferences: {
      answer: async () => ({
        appearance: DEFAULT_PREFERENCES.appearance,
        notifications: DEFAULT_PREFERENCES.notifications,
        archived: [],
      }),
    },
    newRun: {
      answer: async request => {
        events.push(`new-run:${request.kind}`);
        return {kind: 'unsupported'};
      },
    },
    runWindows,
    shellUrl: SHELL_URL,
    welcomeIpc: ipc,
    openExternal: async url => {
      opened.push(url);
    },
    log: message => logs.push(message),
  });
  return {
    opened,
    runWindowList,
    ipc,
    document,
    desktop,
    checkouts,
    launches,
    events,
    logs,
    releaseCleanup,
  };
}

describe('Desktop run windows', () => {
  test('a run opens in its own window once, whose surface is answered until it closes', async () => {
    const w = desktopWorld();
    const run = {host: 'ssh:chelan4', runId: '20261015-163047'};
    const fromRunWindow = (id: number, generation: string, channel: string, ...args: unknown[]) =>
      w.ipc.invokeAs(
        id,
        `${SHELL_URL}?host=ssh%3Achelan4&run=20261015-163047`,
        generation,
        channel,
        ...args,
      );

    await expect(w.ipc.invoke('10:100', WELCOME_CHANNELS.openRunWindow, run)).resolves.toEqual({
      ok: true,
      value: null,
    });
    const [window] = w.runWindowList;
    expect(window?.url).toBe(`${SHELL_URL}?host=ssh%3Achelan4&run=20261015-163047`);
    expect(window?.title).toBe('VibeSys · chelan4 · 20261015-163047');

    // Its surface is answered.
    await expect(fromRunWindow(20, '30:300', WELCOME_CHANNELS.releaseLogs)).resolves.toMatchObject({
      ok: true,
    });

    // Opening the same run again, from either window, focuses its window.
    await fromRunWindow(20, '30:300', WELCOME_CHANNELS.openRunWindow, run);
    expect(w.runWindowList).toHaveLength(1);
    expect(window?.focused).toBe(1);

    // Closed, it is forgotten and refused; opening the run again makes a new window.
    window?.close();
    await spin();
    await expect(fromRunWindow(20, '30:300', WELCOME_CHANNELS.releaseLogs)).resolves.toMatchObject({
      ok: false,
      error: 'not the welcome view',
    });
    await w.ipc.invoke('10:100', WELCOME_CHANNELS.openRunWindow, run);
    expect(w.runWindowList).toHaveLength(2);
    await expect(fromRunWindow(21, '31:301', WELCOME_CHANNELS.releaseLogs)).resolves.toMatchObject({
      ok: true,
    });
  });
});

describe('Desktop external addresses', () => {
  test('opens a web or mail address from either window, and refuses every other one', async () => {
    const w = desktopWorld();
    const run = {host: 'ssh:chelan4', runId: '20261015-163047'};
    await w.ipc.invoke('10:100', WELCOME_CHANNELS.openRunWindow, run);
    const runWindowUrl = `${SHELL_URL}?host=ssh%3Achelan4&run=20261015-163047`;
    await expect(
      w.ipc.invoke('10:100', WELCOME_CHANNELS.openExternal, 'https://example.com/a'),
    ).resolves.toEqual({ok: true, value: null});
    await expect(
      w.ipc.invokeAs(20, runWindowUrl, '30:300', WELCOME_CHANNELS.openExternal, 'mailto:a@b.co'),
    ).resolves.toEqual({ok: true, value: null});
    expect(w.opened).toEqual(['https://example.com/a', 'mailto:a@b.co']);
    for (const refused of ['javascript:alert(1)', 'file:///etc/passwd', 'app://vibesys/x', 42]) {
      await expect(
        w.ipc.invoke('10:100', WELCOME_CHANNELS.openExternal, refused),
      ).resolves.toMatchObject({ok: false});
    }
    expect(w.opened).toHaveLength(2);
  });

  test('a page that is not the shell cannot open anything', async () => {
    const w = desktopWorld();
    await expect(
      w.ipc.invokeAs(
        99,
        'https://evil.example/',
        '40:400',
        WELCOME_CHANNELS.openExternal,
        'https://example.com',
      ),
    ).resolves.toMatchObject({ok: false});
    expect(w.opened).toEqual([]);
  });
});

describe('Desktop New run routing', () => {
  test('sends each New run request to the New run feature', async () => {
    const w = desktopWorld();
    const inputs = {
      project: '/project',
      task: 'spsc',
      harness: {provider: 'claude', model: 'claude-a'},
      options: {},
      args: [],
    };
    const calls: readonly [string, ...unknown[]][] = [
      [WELCOME_CHANNELS.describeMachine, 'local'],
      [WELCOME_CHANNELS.preflight, 'local', inputs],
      [WELCOME_CHANNELS.examples, 'local'],
      [WELCOME_CHANNELS.createFromExample, 'local', 'queue-rs', '/projects/queue-rs'],
      [WELCOME_CHANNELS.copyOut, 'local', '/repo/project', '/projects/project'],
      [WELCOME_CHANNELS.startRun, 'local', inputs],
      [WELCOME_CHANNELS.helperFeatures, 'local'],
    ];
    for (const [channel, ...args] of calls) {
      await expect(w.ipc.invoke('10:100', channel, ...args)).resolves.toEqual({
        ok: true,
        value: {kind: 'unsupported'},
      });
    }
    expect(w.events.filter(event => event.startsWith('new-run:'))).toEqual([
      'new-run:describe-machine',
      'new-run:preflight',
      'new-run:examples',
      'new-run:create-from-example',
      'new-run:copy-out',
      'new-run:start-run',
      'new-run:helper-features',
    ]);
  });
});

async function spin(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

describe('Desktop launches', () => {
  test('the main window plan opens nothing', async () => {
    const w = desktopWorld();
    await w.desktop.open({kind: 'main-window'});
    expect(w.launches).toEqual([]);
    expect(w.runWindowList).toHaveLength(0);
  });

  test('an instance plan resolves the run, lets it go, and opens it in a window of its own', async () => {
    const w = desktopWorld();
    await w.desktop.open({
      kind: 'instance',
      host: {kind: 'ssh', alias: 'chelan4'},
      instanceId: '0123456789ab',
    });
    expect(w.launches).toEqual([
      'attach:{"kind":"ssh","alias":"chelan4"}:0123456789ab',
      'detach:20261015-163047',
    ]);
    expect(w.runWindowList.map(window => window.url)).toEqual([
      `${SHELL_URL}?host=ssh%3Achelan4&run=20261015-163047`,
    ]);
  });

  test('a start plan starts the run on this machine and opens it in a window of its own', async () => {
    const w = desktopWorld();
    await w.desktop.open({kind: 'start', project: '/project', runArgs: ['--x']});
    expect(w.launches).toEqual(['start:/project', 'detach:run-1']);
    expect(w.runWindowList.map(window => window.url)).toEqual([
      `${SHELL_URL}?host=local&run=run-1`,
    ]);
  });
});

describe('Desktop checkout ownership', () => {
  test('passes the exact renderer generation through a checkout change and its retirement', async () => {
    const w = desktopWorld();

    await expect(
      w.ipc.invoke('10:100', WELCOME_CHANNELS.setCheckout, 'local', '/old', 1),
    ).resolves.toEqual({ok: true, value: '/old'});
    expect(w.checkouts).toEqual([{path: '/old', change: {owner: '10:100', sequence: 1}}]);

    w.document.navigate('10:100');
    const successor = w.ipc.invoke('11:101', WELCOME_CHANNELS.setCheckout, 'local', '/new', 1);
    await spin();
    expect(w.events).toEqual(['cancel-checkout:10:100', 'retire-host:10:100']);
    expect(w.checkouts).toHaveLength(1);

    w.releaseCleanup();
    await expect(successor).resolves.toEqual({ok: true, value: '/new'});
    expect(w.checkouts[1]?.change).toEqual({owner: '11:101', sequence: 1});
  });

  test('a rejected cleanup cannot admit a successor while its sibling is pending', async () => {
    const retirementStarted = new Deferred();
    const retirement = new Deferred();
    const w = desktopWorld({
      cancelCheckoutOwner: async () => {
        throw new Error('checkout cancellation failed');
      },
      retireOwner: async () => {
        retirementStarted.resolve();
        await retirement.promise;
      },
    });
    await w.ipc.invoke('10:100', WELCOME_CHANNELS.setCheckout, 'local', '/old', 1);

    w.document.navigate('10:100');
    const successor = w.ipc.invoke('11:101', WELCOME_CHANNELS.setCheckout, 'local', '/new', 1);
    await retirementStarted.promise;
    await spin();

    expect(w.checkouts).toHaveLength(1);
    expect(w.logs).toEqual([]);
    retirement.resolve();
    await expect(successor).resolves.toEqual({ok: true, value: '/new'});
    expect(w.checkouts).toHaveLength(2);
    expect(w.logs).toEqual([
      'could not retire checkout changes: checkout cancellation: Error: checkout cancellation failed',
    ]);
  });

  test('reports every cleanup rejection after the ownership barrier settles', async () => {
    const w = desktopWorld({
      cancelCheckoutOwner: async () => {
        throw new Error('checkout cancellation failed');
      },
      retireOwner: async () => {
        throw new Error('host retirement failed');
      },
    });
    await w.ipc.invoke('10:100', WELCOME_CHANNELS.releaseLogs);

    w.document.navigate('10:100');
    await expect(w.ipc.invoke('11:101', WELCOME_CHANNELS.releaseLogs)).resolves.toEqual({
      ok: true,
      value: null,
    });

    expect(w.logs).toEqual([
      'could not retire checkout changes: checkout cancellation: Error: checkout cancellation failed; host retirement: Error: host retirement failed',
    ]);
  });
});
