import {describe, expect, test} from 'bun:test';
import {createRequire} from 'node:module';
import type {MainToPage, PageToMain, RunChannelRole} from '@vibesys/ipc';

const createRunPreload = createRequire(import.meta.url)('./run.cjs') as typeof import('./run.cjs');

class FakePort {
  readonly posted: PageToMain[] = [];
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  closed = false;

  postMessage(message: PageToMain): void {
    if (this.closed) throw new Error('posted on a closed port');
    this.posted.push(message);
  }

  close(): void {
    this.closed = true;
  }

  receive(message: MainToPage | unknown): void {
    this.onmessage?.(new MessageEvent('message', {data: message}));
  }
}

class FakePreload {
  readonly platform = 'test';
  readonly ports: FakePort[] = [];
  readonly attached: Array<{readonly remote: object; readonly role: RunChannelRole}> = [];
  #wake: (() => void) | null = null;

  createChannel(): {port: FakePort; remote: object} {
    const port = new FakePort();
    const remote = {};
    this.ports.push(port);
    return {port, remote};
  }

  attach(remote: object, role: RunChannelRole): void {
    this.attached.push({remote, role});
  }

  onWake(listener: () => void): void {
    this.#wake = listener;
  }

  wake(): void {
    this.#wake?.();
  }
}

function connection() {
  const capabilities = new FakePreload();
  const opened: string[] = [];
  const messages: string[] = [];
  const closed: string[] = [];
  const bridge = createRunPreload(capabilities);
  const socket = bridge.connect('run-control', {
    onOpen: () => opened.push('open'),
    onMessage: data => messages.push(data),
    onClose: () => closed.push('close'),
  });
  const port = capabilities.ports[0];
  if (port === undefined) throw new Error('connect did not create a port');
  return {capabilities, port, socket, opened, messages, closed};
}

describe('run preload bridge', () => {
  test('exposes platform, connection attachment, wake, and exact incoming byte credit', () => {
    const capabilities = new FakePreload();
    const bridge = createRunPreload(capabilities);
    let woke = false;
    bridge.onWake(() => {
      woke = true;
    });
    capabilities.wake();

    const messages: string[] = [];
    const socket = bridge.connect('subscribe', {
      onOpen: () => undefined,
      onMessage: data => messages.push(data),
      onClose: () => undefined,
    });
    const port = capabilities.ports[0];
    if (port === undefined) throw new Error('connect did not create a port');
    port.receive({type: 'open', creditBytes: 3});
    port.receive({type: 'message', data: 'é', bytes: 3});

    expect(bridge.platform).toBe('test');
    expect(woke).toBe(true);
    expect(capabilities.attached).toHaveLength(1);
    expect(capabilities.attached[0]?.role).toBe('subscribe');
    expect(messages).toEqual(['é']);
    expect(port.posted).toEqual([
      {type: 'credit', bytes: 3},
      {type: 'credit', bytes: 3},
    ]);
    socket.close();
  });

  test('queues within the window and flushes frames in order as credit returns', () => {
    const {port, socket, opened} = connection();
    port.receive({type: 'open', creditBytes: 6});

    socket.send('é');
    socket.send('cde');
    expect(port.posted).toEqual([
      {type: 'credit', bytes: 6},
      {type: 'send', data: 'é'},
    ]);

    port.receive({type: 'credit', bytes: 3});
    expect(port.posted).toEqual([
      {type: 'credit', bytes: 6},
      {type: 'send', data: 'é'},
      {type: 'send', data: 'cde'},
    ]);
    expect(opened).toEqual(['open']);
  });

  test('a malformed initial credit closes without opening the page socket', () => {
    const {port, opened, closed} = connection();

    port.receive({type: 'open', creditBytes: 0});

    expect(port.posted).toEqual([{type: 'close'}]);
    expect(port.closed).toBe(true);
    expect(opened).toEqual([]);
    expect(closed).toEqual(['close']);
  });

  test('fails closed instead of growing the page queue beyond one window', () => {
    const {port, socket, closed} = connection();
    port.receive({type: 'open', creditBytes: 5});
    socket.send('xxxx');
    socket.send('yyyy');

    socket.send('z');

    expect(port.posted).toEqual([
      {type: 'credit', bytes: 5},
      {type: 'send', data: 'xxxx'},
      {type: 'close'},
    ]);
    expect(port.closed).toBe(true);
    expect(closed).toEqual(['close']);
  });

  for (const [name, message] of [
    ['a duplicate open', {type: 'open', creditBytes: 5}],
    ['excess returned credit', {type: 'credit', bytes: 1}],
    ['a malformed frame byte count', {type: 'message', data: 'é', bytes: 2}],
    ['an unknown message', {type: 'unknown'}],
  ] as const) {
    test(`${name} closes the port and reports one close`, () => {
      const {port, closed} = connection();
      port.receive({type: 'open', creditBytes: 5});

      port.receive(message);

      expect(port.posted.at(-1)).toEqual({type: 'close'});
      expect(port.closed).toBe(true);
      expect(closed).toEqual(['close']);
    });
  }

  test('normal multi-line delivery and an intentional close settle once', () => {
    const {port, socket, messages, closed} = connection();
    port.receive({type: 'open', creditBytes: 8});
    port.receive({type: 'message', data: 'one', bytes: 4});
    port.receive({type: 'message', data: 'two', bytes: 4});
    expect(messages).toEqual(['one', 'two']);

    socket.close();
    port.receive({type: 'close'});

    expect(port.posted.at(-1)).toEqual({type: 'close'});
    expect(port.closed).toBe(true);
    expect(closed).toEqual([]);
  });
});
