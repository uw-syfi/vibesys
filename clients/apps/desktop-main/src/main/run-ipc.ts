/**
 * Admission for the run page's privileged protocol connection.
 *
 * Electron wiring translates an incoming MessagePort into `PendingRunPort`; this module owns the
 * decision to start and expose that port, or close it without handing it to a run page.
 *
 * `ipcMain` is one process-wide channel, shared by every open window: a connect Electron delivers
 * to it is not scoped to the window whose run page actually sent it. Admission is therefore one
 * registry of competing windows, not one independent listener per window — a second window's
 * listener would otherwise see the first window's rightfully admitted connect, fail its own
 * sender-id check, and close the port out from under the window that just started it.
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

/** One window's admission claim: its current run page, and where it hands off an admitted port. */
export interface RunPageAdmission {
  /** This window's current run-page webContents id, or null while it shows none. */
  currentSenderId(): number | null;
  /** Hand an admitted port to this window's run. */
  connect(port: RelayPort): void;
}

/**
 * The one `ipcMain` listener this process installs for `expectedUrl`'s connect channel.
 *
 * Every open window registers its admission for as long as it exists; `register`'s return value
 * withdraws it, which every registrant must call exactly once. A connect is admitted by whichever
 * registered window's current run page sent it; a port no registered window claims is refused and
 * closed here, never handed to more than one window and never silently dropped.
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
  register(page: RunPageAdmission): () => void {
    this.#pages.add(page);
    return () => this.#pages.delete(page);
  }

  #admit(event: RunConnectEvent): void {
    const port = event.port;
    if (port === null) return;
    if (isSameAppPage(event.senderUrl, this.#expectedUrl)) {
      for (const page of this.#pages) {
        if (page.currentSenderId() !== event.senderId) continue;
        port.start();
        page.connect(port);
        return;
      }
    }
    this.#log('refused a connection from a view that is not bound to a run');
    port.close();
  }
}
