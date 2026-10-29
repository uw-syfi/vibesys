/**
 * Admission for a shell document's privileged protocol connection to its run.
 *
 * Electron wiring translates an incoming MessagePort into `PendingRunPort`; this module owns the
 * decision to start and expose that port, or close it without handing it to a window.
 *
 * `ipcMain` is one process-wide channel, shared by every open window: a connect Electron delivers
 * to it is not scoped to the window whose document actually sent it. Admission is therefore one
 * registry of competing windows, not one independent listener per window — a second window's
 * listener would otherwise see the first window's rightfully admitted connect, fail its own
 * sender-id check, and close the port out from under the window that just started it.
 */
import type {RelayPort} from '@vibesys/feature-run-control/main';
import {isSameAppPage} from './app-assets.js';

/** A renderer port that has not crossed the run authority boundary yet. */
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

/** One window's admission claim: its run-showing document, and where it hands off an admitted port. */
export interface RunPageAdmission {
  /** This window's run-showing webContents id, or null while its surface binds no run. */
  currentSenderId(): number | null;
  /** Hand an admitted port to this window's run. */
  connect(port: RelayPort): void;
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
 * The one `ipcMain` listener this process installs for the connect channel of the pages that show
 * runs (`expectedUrl`: the app shell, which shows a run in its own document).
 *
 * Every open window registers its admission for as long as it exists; `register`'s return value
 * withdraws it, which every registrant must call exactly once. A connect is admitted by whichever
 * registered window's run-showing document sent it; a port no registered window claims is refused and
 * closed here, never handed to more than one window and never silently dropped. `connect` is called
 * with the admitted port's feature-owned side, with its message and close listeners already
 * installed synchronously; only then does this boundary start Electron's queued delivery.
 */
export class RunIpcRegistry {
  readonly #expectedUrl: string;
  readonly #log: (message: string) => void;
  readonly #pages = new Set<RunPageAdmission>();

  constructor(ipc: RunIpc, expectedUrl: string, log: (message: string) => void) {
    this.#expectedUrl = expectedUrl;
    this.#log = log;
    ipc.onConnect(event => this.#admit(event));
  }

  /** Compete `page`'s admission for every future connect; its return value withdraws it. */
  #expected(senderUrl: string | null): boolean {
    return isSameAppPage(senderUrl, this.#expectedUrl);
  }

  register(page: RunPageAdmission): () => void {
    this.#pages.add(page);
    return () => this.#pages.delete(page);
  }

  #admit(event: RunConnectEvent): void {
    const port = event.port;
    if (port === null) return;
    if (this.#expected(event.senderUrl)) {
      for (const page of this.#pages) {
        if (page.currentSenderId() !== event.senderId) continue;
        this.#activate(port, page);
        return;
      }
    }
    this.#log('refused a connection from a view that is not bound to a run');
    port.close();
  }

  #activate(port: PendingRunPort, page: RunPageAdmission): void {
    let adopted: AdoptedRunPort | null = null;
    try {
      adopted = adopt(port);
      page.connect(adopted.relay);
      adopted.start();
    } catch (error) {
      if (adopted === null) port.close();
      else adopted.dispose();
      this.#log(`could not activate the run connection: ${describe(error)}`);
    }
  }
}
