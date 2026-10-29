import {describe, expect, test} from 'bun:test';
import type {RelayPort} from '@vibesys/feature-run-control/main';
import {SHELL_URL} from './app-assets.js';
import {
  type PendingRunPort,
  type RunConnectEvent,
  type RunIpc,
  RunIpcRegistry,
  type RunPageAdmission,
} from './run-ipc.js';

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

/** One registered page: its claimed sender id, and every port it was ever handed. */
class FakePage implements RunPageAdmission {
  currentId: number | null;
  readonly connected: RelayPort[] = [];

  constructor(currentId: number | null) {
    this.currentId = currentId;
  }

  currentSenderId(): number | null {
    return this.currentId;
  }

  connect(port: RelayPort): void {
    this.connected.push(port);
  }
}

const CLOSED = Symbol('closed');

function expectAdmission(
  ipc: FakeRunIpc,
  page: FakePage,
  senderId: number,
  senderUrl: string | null,
  allowed: boolean,
): void {
  const port = new FakePendingRunPort();
  const connectionsBefore = page.connected.length;
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
  expect({
    senderId,
    senderUrl,
    connectionDelta: page.connected.length - connectionsBefore,
  }).toEqual({senderId, senderUrl, connectionDelta: Number(allowed)});
}

describe('RunIpcRegistry', () => {
  test('admits only the current sender at the shell page', () => {
    const ipc = new FakeRunIpc();
    const refused: string[] = [];
    const registry = new RunIpcRegistry(ipc, SHELL_URL, message => refused.push(message));
    const page = new FakePage(41);
    registry.register(page);

    const pages = [
      {url: SHELL_URL, allowed: true},
      {url: `${SHELL_URL}?source=launch`, allowed: true},
      {url: `${SHELL_URL}#run`, allowed: true},
      {url: 'app://vibesys/welcome.html', allowed: false},
      {url: 'app://vibesys/shell.html-preview.html', allowed: false},
      {url: 'app://vibesys/shell.html/child', allowed: false},
      {url: 'app://other/shell.html', allowed: false},
      {url: 'app://user@vibesys/shell.html', allowed: false},
      {url: 'app://vibesys:443/shell.html', allowed: false},
      {url: 'https://vibesys/shell.html', allowed: false},
      {url: 'not a url', allowed: false},
      {url: null, allowed: false},
    ] as const;

    for (const senderId of [41, 42]) {
      for (const candidate of pages) {
        expectAdmission(
          ipc,
          page,
          senderId,
          candidate.url,
          senderId === page.currentId && candidate.allowed,
        );
      }
    }

    page.currentId = null;
    const detachedPort = new FakePendingRunPort();
    const connectionsBefore = page.connected.length;
    ipc.request({port: detachedPort, senderId: 41, senderUrl: SHELL_URL});
    expect(detachedPort.starts).toBe(0);
    expect(detachedPort.closes).toBe(1);
    expect(page.connected).toHaveLength(connectionsBefore);
    expect(refused).toHaveLength(
      pages.filter(candidate => !candidate.allowed).length + pages.length + 1,
    );
  });

  test('ignores a connection event with no transferred port', () => {
    const ipc = new FakeRunIpc();
    const refused: string[] = [];
    const registry = new RunIpcRegistry(ipc, SHELL_URL, message => refused.push(message));
    const page = new FakePage(41);
    registry.register(page);

    ipc.request({port: null, senderId: 41, senderUrl: SHELL_URL});

    expect(page.connected).toEqual([]);
    expect(refused).toEqual([]);
  });

  test('regression: a second window never steals or closes a port the first window owns', () => {
    const ipc = new FakeRunIpc();
    const refused: string[] = [];
    const registry = new RunIpcRegistry(ipc, SHELL_URL, message => refused.push(message));
    const first = new FakePage(41);
    const second = new FakePage(42);
    registry.register(first);
    registry.register(second);

    const portForFirst = new FakePendingRunPort();
    ipc.request({port: portForFirst, senderId: 41, senderUrl: SHELL_URL});
    expect(portForFirst.starts).toBe(1);
    expect(portForFirst.closes).toBe(0);
    expect(first.connected).toHaveLength(1);
    expect(second.connected).toEqual([]);
    // Admission adopts the raw port behind a feature-owned relay before handing it to the page, so
    // identity from here on is tracked through that relay, not the admitted `PendingRunPort`.
    const firstRelay = first.connected[0];
    if (firstRelay === undefined) throw new Error('the first page did not receive a relay');

    const portForSecond = new FakePendingRunPort();
    ipc.request({port: portForSecond, senderId: 42, senderUrl: SHELL_URL});
    expect(portForSecond.starts).toBe(1);
    expect(portForSecond.closes).toBe(0);
    expect(second.connected).toHaveLength(1);
    // Neither window's earlier admission is disturbed by the other's.
    expect(first.connected).toEqual([firstRelay]);
    expect(refused).toEqual([]);
  });

  test('a port neither registered window claims is refused and closed exactly once', () => {
    const ipc = new FakeRunIpc();
    const refused: string[] = [];
    const registry = new RunIpcRegistry(ipc, SHELL_URL, message => refused.push(message));
    registry.register(new FakePage(41));
    registry.register(new FakePage(42));

    const port = new FakePendingRunPort();
    ipc.request({port, senderId: 99, senderUrl: SHELL_URL});

    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
    expect(refused).toHaveLength(1);
  });

  test('withdrawing a registration stops it from competing for later connects', () => {
    const ipc = new FakeRunIpc();
    const registry = new RunIpcRegistry(ipc, SHELL_URL, () => {});
    const page = new FakePage(41);
    const unregister = registry.register(page);
    unregister();

    const port = new FakePendingRunPort();
    ipc.request({port, senderId: 41, senderUrl: SHELL_URL});

    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
    expect(page.connected).toEqual([]);
  });

  test('adopts the owner before starting queued port delivery', () => {
    const ipc = new FakeRunIpc();
    const messages: unknown[] = [];
    let closes = 0;
    const registry = new RunIpcRegistry(ipc, SHELL_URL, () => {});
    registry.register({
      currentSenderId: () => 41,
      connect: port => {
        port.onMessage(message => messages.push(message));
        port.onClose(() => {
          closes += 1;
          port.close();
        });
      },
    });
    const port = new FakePendingRunPort([{type: 'queued'}, CLOSED]);

    ipc.request({port, senderId: 41, senderUrl: SHELL_URL});

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
    const registry = new RunIpcRegistry(ipc, SHELL_URL, failure => failures.push(failure));
    registry.register({
      currentSenderId: () => 41,
      connect: port => connect(port, owner),
    });
    const port = makePort();

    expect(() => ipc.request({port, senderId: 41, senderUrl: SHELL_URL})).not.toThrow();

    expect(port.starts).toBe(expectedStarts);
    expect(port.closes).toBe(1);
    expect(owner.signal.aborted).toBe(true);
    expect(failures).toHaveLength(1);
    expect(failures[0]).toContain(message);
  });

  test('does not start a port its connection owner rejected', () => {
    const ipc = new FakeRunIpc();
    const registry = new RunIpcRegistry(ipc, SHELL_URL, () => {});
    registry.register({currentSenderId: () => 41, connect: port => port.close()});
    const port = new FakePendingRunPort();

    ipc.request({port, senderId: 41, senderUrl: SHELL_URL});

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
    const registry = new RunIpcRegistry(ipc, SHELL_URL, () => {});
    registry.register({
      currentSenderId: () => 41,
      connect: port => {
        port.onClose(() => {
          ownerCloses += 1;
          port.close();
        });
        port.close();
      },
    });
    const port = new FakePendingRunPort([], null, notification);

    ipc.request({port, senderId: 41, senderUrl: SHELL_URL});
    port.flushDeferredClose();

    expect(ownerCloses).toBe(0);
    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
  });
});
