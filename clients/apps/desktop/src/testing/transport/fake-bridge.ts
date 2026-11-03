/** A `GatewayPortBridge` standing in for the preload: it records each open and cancel, and a test replies by hand. */
import type {GatewayPortBridge} from '@vibesys/ipc';

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

export const makeFakeBridge = (): FakeBridge => {
  interface Port {
    request: Uint8Array;
    cancels: number;
    receive: (reply: Uint8Array) => void;
    failed: (error: string) => void;
  }
  const opened: Port[] = [];
  return {
    bridge: {
      open(request, receive, failed) {
        const port: Port = {request, cancels: 0, receive, failed};
        opened.push(port);
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
