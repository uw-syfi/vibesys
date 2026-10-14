/**
 * Admission for the run page's privileged protocol connection.
 *
 * Electron wiring translates an incoming MessagePort into `PendingRunPort`; this module owns the
 * decision to start and expose that port, or close it without handing it to the run controller.
 */
import type {RelayPort} from '@vibesys/feature-run-control/main';
import {isSameAppPage} from './app-assets.js';

/** A renderer port that has not crossed the run-page authority boundary yet. */
export interface PendingRunPort extends RelayPort {
  start(): void;
}

/** The sender facts Electron supplies for one requested run connection. */
export interface RunConnectEvent {
  readonly port: PendingRunPort | null;
  readonly senderId: number;
  readonly senderUrl: string | null;
}

/** The narrow IPC boundary needed by the run connection binder. */
export interface RunIpc {
  onConnect(listener: (event: RunConnectEvent) => void): void;
}

interface AdoptedRunPort {
  /** The feature-owned side; closing it repeatedly still closes the native port at most once. */
  readonly relay: RelayPort;
  /** Begin queued Electron delivery, unless the feature rejected the port while adopting it. */
  start(): void;
  /** Cancel the admitted owner synchronously, then close the native port exactly once. */
  dispose(): void;
}

/**
 * Transfer listener ownership before queued delivery can begin.
 *
 * Electron owns activation, while the feature owns the relay listeners. This adapter is the one
 * handoff point between them: it records either side's close before delegating it, so rejection,
 * an incoming close, and activation failure all converge on one native close operation.
 */
function adopt(port: PendingRunPort): AdoptedRunPort {
  let nativeClosed = false;
  let ownerClosed = false;
  const closeListeners = new Set<() => void>();
  const closeOwner = (): void => {
    if (ownerClosed) return;
    ownerClosed = true;
    for (const listener of [...closeListeners]) listener();
    closeListeners.clear();
  };
  const relay: RelayPort = {
    post: message => port.post(message),
    onMessage: listener => port.onMessage(listener),
    onClose: listener => closeListeners.add(listener),
    close: () => {
      if (nativeClosed) return;
      nativeClosed = true;
      port.close();
    },
  };
  port.onClose(() => {
    // Electron may acknowledge our own `close()` with this event. Only the peer-originated event
    // owns feature cancellation; local teardown already chose whether to cancel the owner.
    if (nativeClosed) return;
    nativeClosed = true;
    closeOwner();
  });
  return {
    relay,
    start: () => {
      if (!nativeClosed) port.start();
    },
    dispose: () => {
      try {
        closeOwner();
      } finally {
        relay.close();
      }
    },
  };
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Admit connection ports only from the current run view's exact document.
 *
 * `currentSenderId` is read for every event because replacing a run page immediately retires the
 * old WebContents authority. Rejected ports are closed here and never reach `connect`. `connect`
 * synchronously installs the admitted owner's message and close listeners before returning; only
 * then does this boundary start Electron's queued delivery.
 */
export function bindRunIpc(
  ipc: RunIpc,
  expectedUrl: string,
  currentSenderId: () => number | null,
  connect: (port: RelayPort) => void,
  log: (message: string) => void,
): void {
  ipc.onConnect(event => {
    const port = event.port;
    if (port === null) return;
    if (event.senderId !== currentSenderId() || !isSameAppPage(event.senderUrl, expectedUrl)) {
      log('refused a connection from a view that is not bound to a run');
      port.close();
      return;
    }
    let adopted: AdoptedRunPort | null = null;
    try {
      adopted = adopt(port);
      connect(adopted.relay);
      adopted.start();
    } catch (error) {
      if (adopted === null) port.close();
      else adopted.dispose();
      log(`could not activate the run page connection: ${describe(error)}`);
    }
  });
}
