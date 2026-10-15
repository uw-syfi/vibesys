/**
 * The run page's `RunLogSource` inside the desktop shell: each listing and read is one `logs`
 * request through the run preload, which the main process answers from the run this window is
 * bound to (`docs/contributing/desktop-connection.md`, "Logs"). In a plain browser there is no
 * bridge and no source, so the page shows no startup log panel. The requests themselves run in the
 * backend adapter (`bridgeLogSource`).
 */
import type {RunLogSource} from '@vibesys/logs';
import {bridgeLogSource} from './backend.js';
import {desktopShellAttributes} from './desktop-shell.js';

/** The run page's log source, or null when `bridge` is not a desktop shell that serves logs. */
export function desktopLogSource(bridge: unknown): RunLogSource | null {
  if (desktopShellAttributes(bridge) === null) return null;
  const logs: unknown = (bridge as {readonly logs?: unknown}).logs;
  if (typeof logs !== 'function') return null;
  // A synchronous throw from the bridge becomes a rejection, which the source answers as a failure.
  return bridgeLogSource(request => new Promise(resolve => resolve(logs(request))));
}
