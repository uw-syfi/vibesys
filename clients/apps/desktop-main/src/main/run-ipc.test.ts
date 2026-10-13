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

function expectAdmission(
  ipc: FakeRunIpc,
  connected: readonly RelayPort[],
  senderId: number,
  senderUrl: string | null,
  allowed: boolean,
): void {
  const port = new FakePendingRunPort();
  const connectionsBefore = connected.length;
  ipc.request({port, senderId, senderUrl, value: 'run-control'});
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
  expect(connected.includes(port)).toBe(allowed);
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
      (port, _role) => connected.push(port),
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
    ipc.request({
      port: detachedPort,
      senderId: 41,
      senderUrl: APP_ENTRY_URL,
      value: 'run-control',
    });
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
      (port, _role) => connected.push(port),
      message => refused.push(message),
    );

    ipc.request({
      port: null,
      senderId: 41,
      senderUrl: APP_ENTRY_URL,
      value: 'run-control',
    });

    expect(connected).toEqual([]);
    expect(refused).toEqual([]);
  });

  test('rejects an unknown run channel role before starting the port', () => {
    const ipc = new FakeRunIpc();
    const connected: Array<{readonly port: RelayPort; readonly role: string}> = [];
    const refused: string[] = [];
    bindRunIpc(
      ipc,
      APP_ENTRY_URL,
      () => 41,
      (port, role) => connected.push({port, role}),
      message => refused.push(message),
    );
    const port = new FakePendingRunPort();

    ipc.request({port, senderId: 41, senderUrl: APP_ENTRY_URL, value: 'unknown-role'});

    expect(connected).toEqual([]);
    expect(port.starts).toBe(0);
    expect(port.closes).toBe(1);
    expect(refused).toHaveLength(1);
  });
});
