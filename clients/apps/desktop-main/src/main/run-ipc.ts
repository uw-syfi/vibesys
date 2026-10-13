/**
 * Admission for the run page's privileged protocol connection.
 *
 * Electron wiring translates an incoming MessagePort into `PendingRunPort`; this module owns the
 * decision to start and expose that port, or close it without handing it to the run controller.
 */
import type {RelayPort} from '@vibesys/feature-run-control/main';
import type {RunChannelRole} from '@vibesys/ipc';
import {parseRunChannelRole} from '@vibesys/transport';
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
  readonly value: unknown;
}

/** The narrow IPC boundary needed by the run connection binder. */
export interface RunIpc {
  onConnect(listener: (event: RunConnectEvent) => void): void;
}

/**
 * Admit connection ports only from the current run view's exact document.
 *
 * `currentSenderId` is read for every event because replacing a run page immediately retires the
 * old WebContents authority. Rejected ports are closed here and never reach `connect`.
 */
export function bindRunIpc(
  ipc: RunIpc,
  expectedUrl: string,
  currentSenderId: () => number | null,
  connect: (port: RelayPort, role: RunChannelRole) => void,
  log: (message: string) => void,
): void {
  ipc.onConnect(event => {
    const port = event.port;
    if (port === null) return;
    const role = parseRunChannelRole(event.value);
    if (
      role === null ||
      event.senderId !== currentSenderId() ||
      !isSameAppPage(event.senderUrl, expectedUrl)
    ) {
      log('refused a connection from a view that is not bound to a run');
      port.close();
      return;
    }
    port.start();
    connect(port, role);
  });
}
