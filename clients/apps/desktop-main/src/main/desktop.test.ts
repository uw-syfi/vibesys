import {describe, expect, test} from 'bun:test';
import {WELCOME_CHANNELS, type WelcomeEvent, type WelcomeResult} from '@vibesys/ipc';
import type {RecentFile} from '@vibesys/settings';
import {Desktop, type DesktopDeps} from './desktop.js';
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
    const handler = this.#handlers.get(channel);
    if (handler === undefined) throw new Error(`no handler for ${channel}`);
    return handler(
      {
        senderId: 7,
        senderUrl: 'app://vibesys/welcome.html',
        rendererGeneration,
      },
      ...args,
    );
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
  const starts: Parameters<DesktopDeps['runs']['start']>[] = [];
  const cancels: Parameters<DesktopDeps['runs']['cancelStart']>[] = [];
  let releaseCleanup = (): void => {};
  const cleanup = new Promise<void>(resolve => {
    releaseCleanup = resolve;
  });
  const document = new FakeWelcomeDocument();
  const ipc = new FakeWelcomeIpc();
  const runs: DesktopDeps['runs'] = {
    attached: () => null,
    stops: () => ({}),
    attach: async () => undefined,
    start: async (...args) => {
      starts.push(args);
      return 'started';
    },
    cancelStart: (...args) => {
      cancels.push(args);
    },
    retireStartOwner: owner => {
      events.push(`retire-start:${owner}`);
    },
    resume: async () => undefined,
    stop: async () => undefined,
    retry: () => undefined,
  };
  const hosts: DesktopDeps['hosts'] = {
    aliases: async () => [],
    host: async id => ({
      key: id.kind === 'local' ? 'local' : `ssh:${id.alias}`,
      label: id.kind === 'local' ? 'This Mac' : id.alias,
      checkout: null,
    }),
    setCheckout: async (_id, path) => String(path),
    cancelCheckout: async () => undefined,
    cancelCheckoutOwner: async owner => {
      events.push(`cancel-checkout:${owner}`);
      if (cleanups.cancelCheckoutOwner !== undefined) {
        await cleanups.cancelCheckoutOwner(owner);
        return;
      }
      await cleanup;
    },
    cancelInspection: () => undefined,
    cancelInspectionOwner: () => undefined,
    complete: async (_id, directory) => ({
      kind: 'listed',
      directory,
      entries: [],
      truncated: false,
    }),
    harnesses: async () => ({kind: 'unsupported'}),
    inspect: async (_id, instanceId) => ({
      id: instanceId,
      state: 'ended',
      record: null,
      metadata: null,
      control_socket_path: '/tmp/control.sock',
      server_log_path: '/tmp/server.log',
    }),
    logs: async () => ({ok: true, instanceId: '000000000001', streams: []}),
    recent: async () => undefined,
    releaseLogs: () => undefined,
    retireInspectionOwner: owner => {
      events.push(`retire-inspection:${owner}`);
    },
    retireOwner: async owner => {
      events.push(`retire-host:${owner}`);
      if (cleanups.retireOwner !== undefined) {
        await cleanups.retireOwner(owner);
        return;
      }
      await cleanup;
    },
    settingsProblem: async () => null,
    signIn: async () => undefined,
    tasks: async () => [],
  };
  const recent: RecentFile = {version: 3, runs: []};
  const settings: DesktopDeps['settings'] = {
    onRecentChanged: () => () => undefined,
    recent: async () => recent,
  };
  const window: DesktopDeps['window'] = {
    mode: 'welcome',
    welcome: document,
    focus: () => undefined,
    setMode: () => undefined,
    sendWelcome: <T>(_channel: string, _event: WelcomeEvent<T>): void => undefined,
  };
  new Desktop({
    window,
    welcome: new WelcomeFeed(),
    hosts,
    runs,
    settings,
    welcomeIpc: ipc,
    log: message => logs.push(message),
  });
  return {ipc, document, starts, cancels, events, logs, releaseCleanup};
}

async function spin(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

describe('Desktop welcome start ownership', () => {
  test('passes the exact renderer generation through start, cancel, and synchronous retirement', async () => {
    const w = desktopWorld();

    const harness = {provider: 'claude', model: 'claude-a'};
    await expect(
      w.ipc.invoke(
        '10:100',
        WELCOME_CHANNELS.start,
        'local',
        '/project',
        'spsc',
        '--rounds 2',
        harness,
        1,
      ),
    ).resolves.toEqual({ok: true, value: 'started'});
    await expect(w.ipc.invoke('10:100', WELCOME_CHANNELS.cancelStart, 1)).resolves.toEqual({
      ok: true,
      value: null,
    });

    expect(w.starts).toEqual([
      [
        {kind: 'local'},
        '/project',
        'spsc',
        ['--rounds', '2'],
        {owner: '10:100', sequence: 1},
        harness,
      ],
    ]);
    expect(w.cancels).toEqual([[{owner: '10:100', sequence: 1}]]);

    w.document.navigate('10:100');
    expect(w.events).toEqual(['retire-inspection:10:100', 'retire-start:10:100']);
    const successor = w.ipc.invoke(
      '11:101',
      WELCOME_CHANNELS.start,
      'local',
      '/project',
      'spsc',
      '--rounds 3',
      null,
      1,
    );
    await spin();
    expect(w.events).toEqual([
      'retire-inspection:10:100',
      'retire-start:10:100',
      'cancel-checkout:10:100',
      'retire-host:10:100',
    ]);
    expect(w.starts).toHaveLength(1);

    w.releaseCleanup();
    await expect(successor).resolves.toEqual({ok: true, value: 'started'});
    expect(w.starts[1]?.[4]).toEqual({owner: '11:101', sequence: 1});
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
    await w.ipc.invoke(
      '10:100',
      WELCOME_CHANNELS.start,
      'local',
      '/project',
      'spsc',
      '--rounds 2',
      null,
      1,
    );

    w.document.navigate('10:100');
    const successor = w.ipc.invoke(
      '11:101',
      WELCOME_CHANNELS.start,
      'local',
      '/project',
      'spsc',
      '--rounds 3',
      null,
      1,
    );
    await retirementStarted.promise;
    await spin();

    expect(w.starts).toHaveLength(1);
    expect(w.logs).toEqual([]);
    retirement.resolve();
    await expect(successor).resolves.toEqual({ok: true, value: 'started'});
    expect(w.starts).toHaveLength(2);
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
    await w.ipc.invoke('10:100', WELCOME_CHANNELS.hosts);

    w.document.navigate('10:100');
    await expect(w.ipc.invoke('11:101', WELCOME_CHANNELS.hosts)).resolves.toEqual({
      ok: true,
      value: [],
    });

    expect(w.logs).toEqual([
      'could not retire checkout changes: checkout cancellation: Error: checkout cancellation failed; host retirement: Error: host retirement failed',
    ]);
  });
});
