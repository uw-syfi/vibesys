import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event, eventBatch, snapshotResponse} from '@vibesys/contracts/testing';
import type {DesktopSocketHandlers, RunChannelRole} from '@vibesys/ipc';
import {type BrowserLifecycle, WebSession} from '@vibesys/run-session';
import {FakeLifecycle} from '@vibesys/run-session/testing';
import type {ScheduleTimeout} from '@vibesys/transport';
import {
  desktopBridge,
  desktopSessionOptions,
  desktopShellAttributes,
  markDesktopShell,
} from './desktop-shell.js';

describe('desktopShellAttributes', () => {
  test('describes every supported platform', () => {
    for (const platform of ['darwin', 'win32', 'linux'] as const) {
      expect(desktopShellAttributes({platform})).toEqual({
        'data-shell': 'desktop',
        'data-platform': platform,
      });
    }
  });

  test('leaves a plain browser and malformed bridges unmarked', () => {
    const rejected: unknown[] = [
      undefined,
      null,
      'darwin',
      42,
      {},
      {platform: 'freebsd'},
      {platform: 7},
      {platform: null},
    ];
    for (const bridge of rejected) expect(desktopShellAttributes(bridge)).toBeNull();
  });
});

describe('markDesktopShell', () => {
  test('sets the attributes on the root only inside the desktop shell', () => {
    const attributes = new Map<string, string>();
    const root = {setAttribute: (name: string, value: string) => attributes.set(name, value)};
    markDesktopShell(root as unknown as HTMLElement, undefined);
    expect(attributes.size).toBe(0);
    markDesktopShell(root as unknown as HTMLElement, {platform: 'darwin'});
    expect(Object.fromEntries(attributes)).toEqual({
      'data-shell': 'desktop',
      'data-platform': 'darwin',
    });
  });
});

/**
 * The desktop shell as the page sees it: the preload's bridge, the main process's relay, and a
 * server, collapsed into one in-memory object. Each `connect` is one protocol connection that
 * opens asynchronously and answers `query.snapshot` and `subscribe` from `events`.
 */
class FakeShell {
  readonly events: RunEvent[] = [];
  connections = 0;
  readonly roles: RunChannelRole[] = [];
  readonly #open = new Set<DesktopSocketHandlers>();
  readonly #wake = new Set<() => void>();

  readonly bridge = {
    platform: 'darwin',
    connect: (role: RunChannelRole, handlers: DesktopSocketHandlers) => {
      this.connections += 1;
      this.roles.push(role);
      let open = false;
      queueMicrotask(() => {
        open = true;
        this.#open.add(handlers);
        handlers.onOpen();
      });
      return {
        send: (data: string) => {
          if (!open) throw new Error('sent before open');
          this.#answer(handlers, JSON.parse(data) as Record<string, unknown>);
        },
        close: () => {
          this.#open.delete(handlers);
        },
      };
    },
    onWake: (listener: () => void) => {
      this.#wake.add(listener);
    },
  };

  /** Every open connection drops, as when the transport to the server breaks. */
  dropAll(): void {
    const open = [...this.#open];
    this.#open.clear();
    for (const handlers of open) handlers.onClose();
  }

  wake(): void {
    for (const listener of this.#wake) listener();
  }

  #answer(handlers: DesktopSocketHandlers, request: Record<string, unknown>): void {
    const reply = (message: unknown): void =>
      queueMicrotask(() => {
        if (this.#open.has(handlers)) handlers.onMessage(JSON.stringify(message));
      });
    const latest = this.events.length;
    if (request['type'] === 'query.snapshot') {
      reply({
        ...snapshotResponse({sequence: 0}),
        protocol_version: 1,
        request_id: request['request_id'],
      });
    } else if (request['type'] === 'query.iterations') {
      reply({
        protocol_version: 1,
        request_id: request['request_id'],
        timestamp: '2026-01-01T00:00:00Z',
        ok: true,
        iterations_ready: false,
      });
    } else if (request['type'] === 'subscribe') {
      const after = Number(request['after_sequence']);
      reply({
        type: 'subscribed',
        request_id: request['request_id'],
        run_id: 'run-1',
        latest_sequence: latest,
      });
      reply(
        eventBatch(this.events.slice(after), {
          through_sequence: latest,
          store_id: 'store-1',
          history_after_sequence: after,
        }),
      );
    }
  }
}

const visible: BrowserLifecycle = {
  visibilityState: 'visible',
  online: true,
  addEventListener: () => {},
  removeEventListener: () => {},
};

/** A scheduler whose timers never fire: only events the test causes move the session. */
const frozen: ScheduleTimeout = () => () => {};

function sequenceReaches(session: WebSession, sequence: number): Promise<void> {
  return new Promise(resolve => {
    const check = (): void => {
      if (session.store.getState().sequence >= sequence) resolve();
    };
    session.store.subscribe(check);
    check();
  });
}

describe('desktopBridge', () => {
  test('accepts only a recognized shell that carries connect and onWake', () => {
    const shell = new FakeShell();
    expect(desktopBridge(shell.bridge)).not.toBeNull();
    const {connect: _connect, ...withoutConnect} = shell.bridge;
    for (const bridge of [undefined, {platform: 'darwin'}, withoutConnect]) {
      expect(desktopBridge(bridge)).toBeNull();
    }
  });
});

describe('desktop session', () => {
  test('streams the run through bridge connections with the browser transport', async () => {
    const shell = new FakeShell();
    shell.events.push(event(1, 'server_ready'), event(2, 'agent_output_chunk', 'hello'));
    const bridge = desktopBridge(shell.bridge);
    if (bridge === null) throw new Error('bridge rejected');
    const session = new WebSession(
      desktopSessionOptions(bridge, visible, {scheduleTimeout: frozen, reconnectDelaysMs: [0]}),
    );
    await session.start();
    await sequenceReaches(session, 2);
    expect(session.getState()).toMatchObject({status: 'connected', error: null});
    // One connection for the control channel, one for the event stream.
    expect(shell.connections).toBe(2);
    expect([...shell.roles].sort()).toEqual(['run-control', 'subscribe']);
    await session.close();
  });

  test('a shell wake is one reachable attempt without weakening browser reachability', () => {
    const shell = new FakeShell();
    const browser = new FakeLifecycle();
    browser.setOnline(false);
    const bridge = desktopBridge(shell.bridge);
    if (bridge === null) throw new Error('bridge rejected');
    const {lifecycle} = desktopSessionOptions(bridge, browser);
    const online: boolean[] = [];
    const visibility: boolean[] = [];
    lifecycle.addEventListener('online', () => online.push(lifecycle.online));
    lifecycle.addEventListener('visibilitychange', () => visibility.push(lifecycle.online));

    expect(lifecycle.online).toBe(false);
    shell.wake();
    expect(online).toEqual([true]);
    expect(lifecycle.online).toBe(false);

    browser.setVisibility('hidden');
    browser.setVisibility('visible');
    expect(visibility).toEqual([false, false]);
    browser.setOnline(true);
    expect(online).toEqual([true, true]);
    expect(lifecycle.online).toBe(true);
  });

  test('an offline shell wake redials a dropped stream and resumes after the last event', async () => {
    const shell = new FakeShell();
    const lifecycle = new FakeLifecycle();
    lifecycle.setOnline(false);
    shell.events.push(event(1, 'server_ready'));
    const bridge = desktopBridge(shell.bridge);
    if (bridge === null) throw new Error('bridge rejected');
    const options = desktopSessionOptions(bridge, lifecycle, {
      scheduleTimeout: frozen,
      reconnectDelaysMs: [0],
    });
    const session = new WebSession(options);
    await session.start();
    await sequenceReaches(session, 1);

    shell.dropAll();
    shell.events.push(event(2, 'agent_output_chunk', 'after sleep'));
    const dialsBeforeWake = shell.connections;
    // The redial timer never fires, so only the wake can bring the stream back.
    shell.wake();
    await sequenceReaches(session, 2);
    expect(shell.connections).toBeGreaterThan(dialsBeforeWake);
    expect(options.lifecycle.online).toBe(false);
    await session.close();
  });
});
