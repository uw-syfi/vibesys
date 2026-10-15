import {describe, expect, test} from 'bun:test';
import type {RelayPort} from '@vibesys/feature-run-control/main';
import {APP_ENTRY_URL} from './app-assets.js';
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

  start(): void {
    this.starts += 1;
  }

  post(_message: unknown): void {}

  onMessage(_listener: (message: unknown) => void): void {}

  onClose(_listener: () => void): void {}

  close(): void {
    this.closes += 1;
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
  expect(page.connected.includes(port)).toBe(allowed);
}

describe('RunIpcRegistry', () => {
  test('admits only the current sender at the exact run resource', () => {
    const ipc = new FakeRunIpc();
    const refused: string[] = [];
    const registry = new RunIpcRegistry(ipc, APP_ENTRY_URL, message => refused.push(message));
    const page = new FakePage(41);
    registry.register(page);

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
    ipc.request({port: detachedPort, senderId: 41, senderUrl: APP_ENTRY_URL});
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
    const registry = new RunIpcRegistry(ipc, APP_ENTRY_URL, message => refused.push(message));
    const page = new FakePage(41);
    registry.register(page);

    ipc.request({port: null, senderId: 41, senderUrl: APP_ENTRY_URL});

    expect(page.connected).toEqual([]);
    expect(refused).toEqual([]);
  });

  test('regression: a second window never steals or closes a port the first window owns', () => {
    const ipc = new FakeRunIpc();
    const refused: string[] = [];
    const registry = new RunIpcRegistry(ipc, APP_ENTRY_URL, message => refused.push(message));
    const first = new FakePage(41);
    const second = new FakePage(42);
    registry.register(first);
    registry.register(second);

    const portForFirst = new FakePendingRunPort();
    ipc.request({port: portForFirst, senderId: 41, senderUrl: APP_ENTRY_URL});
    expect(portForFirst.starts).toBe(1);
    expect(portForFirst.closes).toBe(0);
    expect(first.connected).toEqual([portForFirst]);
    expect(second.connected).toEqual([]);

    const portForSecond = new FakePendingRunPort();
    ipc.request({port: portForSecond, senderId: 42, senderUrl: APP_ENTRY_URL});
    expect(portForSecond.starts).toBe(1);
    expect(portForSecond.closes).toBe(0);
    expect(second.connected).toEqual([portForSecond]);
    // Neither window's earlier admission is disturbed by the other's.
    expect(first.connected).toEqual([portForFirst]);
    expect(refused).toEqual([]);
  });

  test('a port neither registered window claims is refused and closed exactly once', () => {
    const ipc = new FakeRunIpc();
    const refused: string[] = [];
    const registry = new RunIpcRegistry(ipc, APP_ENTRY_URL, message => refused.push(message));
    registry.register(new FakePage(41));
    registry.register(new FakePage(42));

    const port = new FakePendingRunPort();
    ipc.request({port, senderId: 99, senderUrl: APP_ENTRY_URL});

    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
    expect(refused).toHaveLength(1);
  });

  test('withdrawing a registration stops it from competing for later connects', () => {
    const ipc = new FakeRunIpc();
    const registry = new RunIpcRegistry(ipc, APP_ENTRY_URL, () => {});
    const page = new FakePage(41);
    const unregister = registry.register(page);
    unregister();

    const port = new FakePendingRunPort();
    ipc.request({port, senderId: 41, senderUrl: APP_ENTRY_URL});

    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
    expect(page.connected).toEqual([]);
  });
});
