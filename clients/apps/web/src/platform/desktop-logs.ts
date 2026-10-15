/**
 * The run page's `RunLogSource` inside the desktop shell: each listing and read is one `logs`
 * request through the run preload, which the main process answers from the run this window is
 * bound to (`docs/contributing/desktop-connection.md`, "Logs"). In a plain browser there is no
 * bridge and no source, so the page shows no startup log panel.
 *
 * An IPC request cannot be withdrawn, so an aborted request rejects at once and its late answer is
 * dropped; the main process's own request ends by its wait bound or when the run's page goes away.
 */
import type {RunLogsRequest} from '@vibesys/ipc';
import type {LogListOutcome, LogReadOutcome, RunLogSource} from '@vibesys/logs';
import {type AbortSignalLike, abortReason, registerAbort} from '@vibesys/transport';
import {desktopShellAttributes} from './desktop-shell.js';

type Ask = (request: RunLogsRequest) => Promise<unknown>;

function isOutcome(value: unknown): value is {readonly ok: boolean} {
  return (
    typeof value === 'object' && value !== null && typeof Reflect.get(value, 'ok') === 'boolean'
  );
}

/** Ask `request`, resolving a refusal or a malformed answer as a `transport` failure. */
function ask<T extends LogListOutcome | LogReadOutcome>(
  logs: Ask,
  request: RunLogsRequest,
  signal: AbortSignalLike,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const registration = registerAbort(signal, () => reject(abortReason(signal)));
    if (registration.aborted) {
      reject(abortReason(signal));
      return;
    }
    const failed = (message: string): T =>
      ({ok: false, failure: {code: 'transport', message}}) as T;
    logs(request).then(
      answer => {
        registration.detach();
        resolve(isOutcome(answer) ? (answer as T) : failed('the desktop app sent no log answer'));
      },
      (error: unknown) => {
        registration.detach();
        resolve(failed(error instanceof Error ? error.message : String(error)));
      },
    );
  });
}

/** The run page's log source, or null when `bridge` is not a desktop shell that serves logs. */
export function desktopLogSource(bridge: unknown): RunLogSource | null {
  if (desktopShellAttributes(bridge) === null) return null;
  const logs: unknown = (bridge as {readonly logs?: unknown}).logs;
  if (typeof logs !== 'function') return null;
  // A synchronous throw from the bridge becomes a rejection, which `ask` answers as a failure.
  const call: Ask = request => new Promise(resolve => resolve(logs(request)));
  return {
    list: signal => ask<LogListOutcome>(call, {kind: 'list'}, signal),
    read: (input, signal) => ask<LogReadOutcome>(call, {kind: 'read', read: input}, signal),
  };
}
