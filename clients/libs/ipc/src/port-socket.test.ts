import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {type MainToPage, openPortSocket, type PagePort, type PageToMain} from './index.js';

const UTF8 = new TextEncoder();
const bytesOf = (data: string): number => UTF8.encode(data).byteLength + 1;

/** The page's port, recording what it posts; `receive` delivers a message from main. */
class FakePort implements PagePort {
  onmessage: ((event: {readonly data: unknown}) => void) | null = null;
  readonly posted: PageToMain[] = [];
  closed = 0;

  postMessage(message: PageToMain): void {
    this.posted.push(message);
  }

  close(): void {
    this.closed += 1;
  }

  receive(data: unknown): void {
    this.onmessage?.({data});
  }
}

function opened() {
  const port = new FakePort();
  const events: string[] = [];
  const socket = openPortSocket(port, {
    onOpen: () => events.push('open'),
    onMessage: data => events.push(`message ${data}`),
    onClose: () => events.push('close'),
  });
  return {port, socket, events};
}

/**
 * One random event from main or the page; returns the most credit it could have granted the page
 * to send (the window on the first open, the bytes of a credit message).
 */
function randomStep(
  random: Seeded,
  port: FakePort,
  socket: ReturnType<typeof openPortSocket>,
  first: boolean,
): number {
  switch (random.int(6)) {
    case 0: {
      const creditBytes = random.pick([0, 5, 20, 64]);
      port.receive({type: 'open', creditBytes} satisfies MainToPage);
      return first ? creditBytes : 0;
    }
    case 1: {
      const bytes = random.pick([1, 3, 8]);
      port.receive({type: 'credit', bytes});
      return bytes;
    }
    case 2: {
      const data = random.pick(['a', 'hello', 'é']);
      port.receive({type: 'message', data, bytes: random.bool() ? bytesOf(data) : 2});
      return 0;
    }
    case 3:
      port.receive(random.pick([null, 'x', {type: 'nope'}]));
      return 0;
    default:
      socket.send(random.pick(['ab', 'cdef', '{"type":"subscribe"}']));
      return 0;
  }
}

describe('openPortSocket', () => {
  test('opens, grants the window, delivers frames and returns their credit', () => {
    const {port, events} = opened();
    port.receive({type: 'open', creditBytes: 10});
    port.receive({type: 'message', data: 'abc', bytes: 4});
    expect(events).toEqual(['open', 'message abc']);
    expect(port.posted).toEqual([
      {type: 'credit', bytes: 10},
      {type: 'credit', bytes: 4},
    ]);
  });

  test('sends within credit and queues the rest until main returns credit', () => {
    const {port, socket} = opened();
    port.receive({type: 'open', creditBytes: 5});
    socket.send('ab');
    socket.send('cd');
    expect(port.posted.filter(message => message.type === 'send')).toEqual([
      {type: 'send', data: 'ab'},
    ]);
    port.receive({type: 'credit', bytes: 3});
    expect(port.posted.filter(message => message.type === 'send')).toHaveLength(2);
  });

  test('a message the protocol does not allow closes once and reports it once', () => {
    const {port, events} = opened();
    port.receive({type: 'message', data: 'early', bytes: 6});
    port.receive({type: 'open', creditBytes: 10});
    expect(events).toEqual(['close']);
    expect(port.closed).toBe(1);
  });

  test('property: sends never exceed the credit main granted, and close is reported once', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const {port, socket, events} = opened();
      let granted = 0;
      let spent = 0;
      for (let step = 0; step < 30; step += 1) {
        const before = port.posted.length;
        granted += randomStep(random, port, socket, granted === 0);
        for (const message of port.posted.slice(before)) {
          if (message.type === 'send') spent += bytesOf(message.data);
        }
        expect(spent, `seed ${seed}`).toBeLessThanOrEqual(granted);
        expect(events.filter(event => event === 'close').length).toBeLessThanOrEqual(1);
      }
    }
  });
});
