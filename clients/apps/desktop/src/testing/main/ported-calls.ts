/**
 * The window's gateway handle over the main process's real call layer: a `GatewayPortBridge` whose
 * every `open` is served by `installGatewayIpc` over `GatewayCalls`, as the preload and Electron's
 * ports do, so a window-side connection and the main-side calls under it meet through the real
 * request and reply messages, and cancelling a call closes its port, which interrupts the call.
 */
import type {GatewayPortBridge} from '../../core/gateway-codec.js';
import {installGatewayIpc, type RunCall} from '../../main/gateway.js';
import type {GatewayCalls} from '../../main/gateway-calls.js';
import type {PortEnd, PortEvent, PortFrame, PortIpc} from '../../main/port-ipc.js';

const FRAME: PortFrame = {processId: 1, routingId: 1, url: 'app://vibesys/index.html'};

/** The main side of one call's port: what the call posts reaches the window, a close ends it. */
class MainPort implements PortEnd {
  readonly #listeners = {message: [] as (() => void)[], close: [] as (() => void)[]};
  constructor(private readonly deliver: (reply: Uint8Array) => void) {}
  on(event: 'message' | 'close', listener: () => void): void {
    this.#listeners[event].push(listener);
  }
  start(): void {}
  postMessage(message: Uint8Array): void {
    this.deliver(message);
  }
  close(): void {}
  /** The window closed its end. */
  closedByWindow(): void {
    for (const listener of this.#listeners.close) listener();
  }
}

export const portedBridge = (calls: GatewayCalls, run: RunCall): GatewayPortBridge => {
  let serve: ((event: PortEvent, value: unknown) => void) | undefined;
  const ipc: PortIpc = {
    on: (_channel, listener) => {
      serve = listener;
    },
  };
  installGatewayIpc(ipc, {calls, admits: () => true, run});
  return {
    open: (request, receive) => {
      const port = new MainPort(receive);
      serve?.(
        {
          ports: [port],
          sender: {
            id: 1,
            mainFrame: FRAME,
            once: () => {},
            on: () => {},
            removeListener: () => {},
          },
          senderFrame: FRAME,
        },
        request,
      );
      return {cancel: () => port.closedByWindow()};
    },
  };
};
