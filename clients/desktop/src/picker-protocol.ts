/**
 * What the host and run picker page and the main process exchange (`ipcRenderer.invoke` channels).
 *
 * The picker is a trusted app page, but the main process still treats its requests as input: a
 * host id must name a host the main process offered, settings are validated before they are saved,
 * and no ssh command line is ever built from these strings directly. The run windows never get
 * these channels; only picker windows do, by the sender's identity.
 */

export const PICKER_CHANNELS = {
  hosts: 'vibesys:picker:hosts',
  runs: 'vibesys:picker:runs',
  signIn: 'vibesys:picker:sign-in',
  attach: 'vibesys:picker:attach',
  start: 'vibesys:picker:start',
  resume: 'vibesys:picker:resume',
  saveSettings: 'vibesys:picker:save-settings',
} as const;

/** `local`, or `ssh:<alias>`. */
export type HostKey = string;

export interface PickerHost {
  readonly key: HostKey;
  readonly label: string;
  /** The host's vibesys command, editable for SSH hosts; null for this machine. */
  readonly vibesysCommand: string | null;
  /** The host's Python command setting; empty when derived from the vibesys command. */
  readonly pythonCommand: string | null;
}

export interface PickerRun {
  readonly id: string;
  readonly projectRoot: string;
  readonly runId: string | null;
  readonly status: string;
  readonly startedAt: number;
  /** Null when the app can attach; otherwise why not (a version mismatch). */
  readonly blocked: string | null;
}

export type PickerResult<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string; readonly authNeeded: boolean};
