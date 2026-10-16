/**
 * The Run screen's live connection inside the app shell: the welcome bridge's `connectRun` turned
 * into the `DesktopBridge` a run page's preload offers, each connection's port arriving as a window
 * message. The main process owns the run's binding and the relay, the app shapes the bridge as the
 * session's transport, and the session machine (`@vibesys/run-session`) owns bootstrap, resume and
 * recovery; this module only adapts the port's arrival.
 */
import {
  type DesktopBridge,
  type DesktopSocket,
  type DesktopSocketHandlers,
  openPortSocket,
  type PagePort,
  RUN_PORT_MESSAGE,
  type WelcomeBridge,
} from '@vibesys/ipc';

/** Where the page hears the preload's window messages. */
export interface PortMessages {
  addEventListener(type: 'message', listener: (event: MessageEvent) => void): void;
  removeEventListener(type: 'message', listener: (event: MessageEvent) => void): void;
}

let connections = 0;

/** A socket whose port has not arrived yet: it opens on the port, or closes the port if closed. */
class PendingSocket implements DesktopSocket {
  #socket: DesktopSocket | null = null;
  #closed = false;

  arrived(port: PagePort, handlers: DesktopSocketHandlers): void {
    if (this.#closed) {
      port.close();
      return;
    }
    this.#socket = openPortSocket(port, handlers);
  }

  send(data: string): void {
    this.#socket?.send(data);
  }

  close(): void {
    this.#closed = true;
    this.#socket?.close();
  }
}

/** A DOM `MessagePort` as the page's side of the relay. */
function pagePort(port: MessagePort): PagePort {
  const page: PagePort = {
    onmessage: null,
    postMessage: message => port.postMessage(message),
    close: () => port.close(),
  };
  port.onmessage = event => page.onmessage?.({data: event.data});
  return page;
}

/** The welcome bridge's run connection as the `DesktopBridge` a run page's preload offers. */
export function welcomeRunBridge(
  bridge: Pick<WelcomeBridge, 'connectRun' | 'onWake'>,
  messages: PortMessages,
): DesktopBridge {
  return {
    connect: handlers => {
      connections += 1;
      const id = `run-page-${connections}`;
      const pending = new PendingSocket();
      const heard = (event: MessageEvent): void => {
        const data = event.data as {readonly type?: unknown; readonly id?: unknown} | null;
        const port = event.ports[0];
        if (data?.type !== RUN_PORT_MESSAGE || data.id !== id || port === undefined) return;
        messages.removeEventListener('message', heard);
        pending.arrived(pagePort(port), handlers);
      };
      messages.addEventListener('message', heard);
      bridge.connectRun(id);
      return pending;
    },
    onWake: listener => bridge.onWake(listener),
  };
}
