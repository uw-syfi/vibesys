/**
 * A `GatewayPortBridge` standing in for the preload: it records each open and cancel. A test
 * replies by hand, or gives a `Recording` to play on each open (the replies arrive during `open`,
 * as a port that already holds them would deliver them).
 */
import {type GatewayPortBridge, gatewayEnd, gatewayReply, parseGatewayRequest} from '@vibesys/ipc';
import type {Recording, Step} from './fake-transport.js';

export interface OpenedPort {
  readonly request: Uint8Array;
  readonly cancels: number;
  /** A reply, as the main process would post it (`gatewayReply` or `gatewayEnd` bytes). */
  readonly reply: (message: unknown) => void;
  /** The port itself failed, with no reply. */
  readonly fail: (message: string) => void;
}

export interface FakeBridge {
  readonly bridge: GatewayPortBridge;
  readonly ports: () => ReadonlyArray<OpenedPort>;
}

/** Play `steps` into a port, up to the first that holds the call open. */
const deliver = (steps: ReadonlyArray<Step>, receive: (reply: Uint8Array) => void): void => {
  for (const step of steps) {
    if ('hang' in step) return;
    if ('frame' in step) {
      receive(gatewayReply(step.frame));
    } else if ('end' in step) {
      receive(gatewayEnd(step.end.message, step.end.status));
      return;
    } else if ('close' in step) {
      receive(gatewayEnd());
      return;
    } else {
      throw new Error('a bridge plays frames, ends, closes and hangs only');
    }
  }
  receive(gatewayEnd());
};

export const makeFakeBridge = (recording?: Recording): FakeBridge => {
  interface Port {
    request: Uint8Array;
    cancels: number;
    receive: (reply: Uint8Array) => void;
    failed: (error: string) => void;
  }
  const opened: Port[] = [];
  const countByMethod = new Map<string, number>();
  return {
    bridge: {
      open(request, receive, failed) {
        const port: Port = {request, cancels: 0, receive, failed};
        opened.push(port);
        if (recording !== undefined) {
          const {host, method, bytes} = parseGatewayRequest(request);
          const nth = countByMethod.get(method) ?? 0;
          countByMethod.set(method, nth + 1);
          deliver(recording.respond({host, method, request: bytes}, nth), receive);
        }
        return {
          cancel: () => {
            port.cancels += 1;
          },
        };
      },
    },
    ports: () =>
      opened.map(port => ({
        request: port.request,
        cancels: port.cancels,
        reply: message => port.receive(message as Uint8Array),
        fail: message => port.failed(message),
      })),
  };
};
