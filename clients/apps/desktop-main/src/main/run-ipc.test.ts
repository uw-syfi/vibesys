import {describe, expect, test} from 'bun:test';
import type {RelayPort} from '@vibesys/feature-run-control/main';
import {APP_ENTRY_URL} from './app-assets.js';
import {bindRunIpc, type PendingRunPort, type RunConnectEvent, type RunIpc} from './run-ipc.js';

class FakeRunIpc implements RunIpc {
  #listener: ((event: RunConnectEvent) => void) | null = null;

  onConnect(listener: (event: RunConnectEvent) => void): void {
    this.#listener = listener;
  }

  request(event: RunConnectEvent): void {
    if (this.#listener === null) throw new Error('run IPC is not bound');
    this.#listener(event);
  }
}

class FakePendingRunPort implements PendingRunPort {
  starts = 0;
  closes = 0;
  readonly #messages = new Set<(message: unknown) => void>();
  readonly #closed = new Set<() => void>();
  readonly #queued: unknown[];
  readonly #startError: Error | null;
  readonly #localCloseNotification: 'none' | 'synchronous' | 'deferred';
  #deferredClose = false;

  constructor(
    queued: readonly unknown[] = [],
    startError: Error | null = null,
    localCloseNotification: 'none' | 'synchronous' | 'deferred' = 'none',
  ) {
    this.#queued = [...queued];
    this.#startError = startError;
    this.#localCloseNotification = localCloseNotification;
  }

  start(): void {
    this.starts += 1;
    if (this.#startError !== null) throw this.#startError;
    for (const message of this.#queued) {
      if (message === CLOSED) {
        this.#notifyClose();
      } else {
        for (const listener of [...this.#messages]) listener(message);
      }
    }
  }

  post(_message: unknown): void {}

  onMessage(listener: (message: unknown) => void): void {
    this.#messages.add(listener);
  }

  onClose(listener: () => void): void {
    this.#closed.add(listener);
  }

  close(): void {
    this.closes += 1;
    if (this.#localCloseNotification === 'synchronous') this.#notifyClose();
    else if (this.#localCloseNotification === 'deferred') this.#deferredClose = true;
  }

  flushDeferredClose(): void {
    if (!this.#deferredClose) return;
    this.#deferredClose = false;
    this.#notifyClose();
  }

  #notifyClose(): void {
    for (const listener of [...this.#closed]) listener();
  }
}

const CLOSED = Symbol('closed');

function expectAdmission(
  ipc: FakeRunIpc,
  connected: readonly RelayPort[],
  senderId: number,
  senderUrl: string | null,
  allowed: boolean,
): void {
  const port = new FakePendingRunPort();
  const connectionsBefore = connected.length;
  ipc.request({port, senderId, senderUrl});
  expect({senderId, senderUrl, starts: port.starts}).toEqual({
    senderId,
    senderUrl,
    starts: Number(allowed),
  });
  expect({senderId, senderUrl, closes: port.closes}).toEqual({
    senderId,
    senderUrl,
    closes: Number(!allowed),
  });
  expect({senderId, senderUrl, connectionDelta: connected.length - connectionsBefore}).toEqual({
    senderId,
    senderUrl,
    connectionDelta: Number(allowed),
  });
}

describe('bindRunIpc', () => {
  test('admits only the current sender at the exact run resource', () => {
    const ipc = new FakeRunIpc();
    const connected: RelayPort[] = [];
    const refused: string[] = [];
    let currentSenderId: number | null = 41;
    bindRunIpc(
      ipc,
      APP_ENTRY_URL,
      () => currentSenderId,
      port => connected.push(port),
      message => refused.push(message),
    );

    const pages = [
      {url: APP_ENTRY_URL, allowed: true},
      {url: `${APP_ENTRY_URL}?source=launch`, allowed: true},
      {url: `${APP_ENTRY_URL}#run`, allowed: true},
      {url: 'app://vibesys/welcome.html', allowed: false},
      {url: 'app://vibesys/desktop.html-preview.html', allowed: false},
      {url: 'app://vibesys/desktop.html/child', allowed: false},
      {url: 'app://other/desktop.html', allowed: false},
      {url: 'app://user@vibesys/desktop.html', allowed: false},
      {url: 'app://vibesys:443/desktop.html', allowed: false},
      {url: 'https://vibesys/desktop.html', allowed: false},
      {url: 'not a url', allowed: false},
      {url: null, allowed: false},
    ] as const;

    for (const senderId of [41, 42]) {
      for (const page of pages) {
        expectAdmission(
          ipc,
          connected,
          senderId,
          page.url,
          senderId === currentSenderId && page.allowed,
        );
      }
    }

    currentSenderId = null;
    const detachedPort = new FakePendingRunPort();
    const connectionsBefore = connected.length;
    ipc.request({port: detachedPort, senderId: 41, senderUrl: APP_ENTRY_URL});
    expect(detachedPort.starts).toBe(0);
    expect(detachedPort.closes).toBe(1);
    expect(connected).toHaveLength(connectionsBefore);
    expect(refused).toHaveLength(pages.filter(page => !page.allowed).length + pages.length + 1);
  });

  test('ignores a connection event with no transferred port', () => {
    const ipc = new FakeRunIpc();
    const connected: RelayPort[] = [];
    const refused: string[] = [];
    bindRunIpc(
      ipc,
      APP_ENTRY_URL,
      () => 41,
      port => connected.push(port),
      message => refused.push(message),
    );

    ipc.request({port: null, senderId: 41, senderUrl: APP_ENTRY_URL});

    expect(connected).toEqual([]);
    expect(refused).toEqual([]);
  });

  test('adopts the owner before starting queued port delivery', () => {
    const ipc = new FakeRunIpc();
    const messages: unknown[] = [];
    let closes = 0;
    bindRunIpc(
      ipc,
      APP_ENTRY_URL,
      () => 41,
      port => {
        port.onMessage(message => messages.push(message));
        port.onClose(() => {
          closes += 1;
          port.close();
        });
      },
      () => {},
    );
    const port = new FakePendingRunPort([{type: 'queued'}, CLOSED]);

    ipc.request({port, senderId: 41, senderUrl: APP_ENTRY_URL});

    expect(messages).toEqual([{type: 'queued'}]);
    expect(closes).toBe(1);
    expect(port.starts).toBe(1);
    expect(port.closes).toBe(0);
  });

  test.each([
    {
      name: 'owner adoption',
      port: () => new FakePendingRunPort(),
      connect: (port: RelayPort, owner: AbortController) => {
        port.onClose(() => owner.abort());
        throw new Error('owner rejected the port');
      },
      expectedStarts: 0,
      message: 'owner rejected the port',
    },
    {
      name: 'queued delivery activation',
      port: () => new FakePendingRunPort([], new Error('port could not start')),
      connect: (port: RelayPort, owner: AbortController) => port.onClose(() => owner.abort()),
      expectedStarts: 1,
      message: 'port could not start',
    },
  ])('closes the port once when $name fails', ({
    port: makePort,
    connect,
    expectedStarts,
    message,
  }) => {
    const ipc = new FakeRunIpc();
    const failures: string[] = [];
    const owner = new AbortController();
    bindRunIpc(
      ipc,
      APP_ENTRY_URL,
      () => 41,
      port => connect(port, owner),
      failure => failures.push(failure),
    );
    const port = makePort();

    expect(() => ipc.request({port, senderId: 41, senderUrl: APP_ENTRY_URL})).not.toThrow();

    expect(port.starts).toBe(expectedStarts);
    expect(port.closes).toBe(1);
    expect(owner.signal.aborted).toBe(true);
    expect(failures).toHaveLength(1);
    expect(failures[0]).toContain(message);
  });

  test('does not start a port its connection owner rejected', () => {
    const ipc = new FakeRunIpc();
    bindRunIpc(
      ipc,
      APP_ENTRY_URL,
      () => 41,
      port => port.close(),
      () => {},
    );
    const port = new FakePendingRunPort();

    ipc.request({port, senderId: 41, senderUrl: APP_ENTRY_URL});

    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
  });

  test.each([
    {notification: 'synchronous'},
    {notification: 'deferred'},
  ] as const)('does not report a $notification local-close notification as a peer close', ({
    notification,
  }) => {
    const ipc = new FakeRunIpc();
    let ownerCloses = 0;
    bindRunIpc(
      ipc,
      APP_ENTRY_URL,
      () => 41,
      port => {
        port.onClose(() => {
          ownerCloses += 1;
          port.close();
        });
        port.close();
      },
      () => {},
    );
    const port = new FakePendingRunPort([], null, notification);

    ipc.request({port, senderId: 41, senderUrl: APP_ENTRY_URL});
    port.flushDeferredClose();

    expect(ownerCloses).toBe(0);
    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
  });
});
