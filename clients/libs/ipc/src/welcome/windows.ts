/**
 * The app shell's window requests: open a run in a window of its own. The main process owns the
 * windows; a view only names the run, and a run window's page reads its run from its URL's query.
 */
import {parseRunRef, type RunRef} from '@vibesys/runs';
import type {WelcomeResult} from './types.js';
import type {ChannelOf} from './values.js';

export const WINDOWS_CHANNELS = {
  openRunWindow: 'vibesys:welcome:open-run-window',
  showMainWindow: 'vibesys:welcome:show-main-window',
  openSettings: 'vibesys:welcome:open-settings',
  openExternal: 'vibesys:welcome:open-external',
} as const;

const MAX_EXTERNAL_URL = 2048;
/**
 * A web address (`http` or `https` with a host) or a mail address (`mailto:` and something), with
 * no space or control character anywhere. This package's neutral runtime has no URL parser, so
 * this is the whole rule; the address is passed on exactly as it was checked.
 */
const EXTERNAL_ADDRESS = /^(?:https?:\/\/[^\s\p{Cc}/?#][^\s\p{Cc}]*|mailto:[^\s\p{Cc}]+)$/iu;

/**
 * `value` when it is a web or mail address the app may hand to the user's own browser or mail app
 * (`http`, `https` or `mailto`, nothing else); throws naming `name` otherwise.
 */
export function parseExternalUrl(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.length > MAX_EXTERNAL_URL) {
    throw new Error(
      `${name} must be a web or mail address of at most ${MAX_EXTERNAL_URL} characters`,
    );
  }
  if (!EXTERNAL_ADDRESS.test(value)) {
    throw new Error(`${name} must be an http, https or mailto address`);
  }
  return value;
}

/** The welcome bridge's window requests. */
export interface WindowsWelcomeBridge {
  /** Show `run` in a window of its own: the one already showing it, or a new one. */
  openRunWindow(run: RunRef): Promise<WelcomeResult<null>>;
  /** Bring the main window (the sidebar and its screens) to the front. */
  showMainWindow(): Promise<WelcomeResult<null>>;
  /** Focus the main window and show its Settings screen. */
  openSettings(): Promise<WelcomeResult<null>>;
  /** Open a web or mail address in the user's own browser or mail app; never in the app. */
  openExternal(url: string): Promise<WelcomeResult<null>>;
  /** Another app window requested Settings. */
  onOpenSettings(listener: () => void): void;
}

export type WindowsRequest =
  | {readonly kind: 'open-run-window'; readonly run: RunRef}
  | {readonly kind: 'show-main-window'}
  | {readonly kind: 'open-settings'}
  | {readonly kind: 'open-external'; readonly url: string};

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseWindowsRequest(
  channel: ChannelOf<typeof WINDOWS_CHANNELS>,
  args: readonly unknown[],
): WindowsRequest {
  switch (channel) {
    case WINDOWS_CHANNELS.openRunWindow:
      return {kind: 'open-run-window', run: parseRunRef(args[0], 'run')};
    case WINDOWS_CHANNELS.showMainWindow:
      return {kind: 'show-main-window'};
    case WINDOWS_CHANNELS.openSettings:
      return {kind: 'open-settings'};
    case WINDOWS_CHANNELS.openExternal:
      return {kind: 'open-external', url: parseExternalUrl(args[0], 'url')};
  }
}

/** The query that names a run window's run in its page's URL. */
const HOST_PARAM = 'host';
const RUN_PARAM = 'run';

/** The app shell's page (`shellUrl`, with no query of its own) showing `run` alone. */
export function runWindowUrl(shellUrl: string, run: RunRef): string {
  const host = encodeURIComponent(run.host);
  const runId = encodeURIComponent(run.runId);
  return `${shellUrl}?${HOST_PARAM}=${host}&${RUN_PARAM}=${runId}`;
}

/**
 * The run a shell page's query (`location.search`) names, or null for the main window (no query);
 * throws naming the offending parameter of a malformed one.
 */
export function runOfWindow(search: string): RunRef | null {
  const query = search.startsWith('?') ? search.slice(1) : search;
  if (query === '') return null;
  const params: Record<string, string> = {};
  for (const pair of query.split('&')) {
    const equals = pair.indexOf('=');
    const name = decodeURIComponent(equals < 0 ? pair : pair.slice(0, equals));
    if (name !== HOST_PARAM && name !== RUN_PARAM) {
      throw new Error(`run window: ${JSON.stringify(name)} is not a known parameter`);
    }
    if (Object.hasOwn(params, name)) throw new Error(`run window: ${name} is given twice`);
    params[name] = equals < 0 ? '' : decodeURIComponent(pair.slice(equals + 1));
  }
  return parseRunRef({host: params[HOST_PARAM], runId: params[RUN_PARAM]}, 'run window');
}
