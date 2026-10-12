/**
 * What the welcome view and the main process exchange: one `ipcRenderer.invoke` channel per
 * request, and the events the main process sends.
 *
 * The requests are grouped by the feature that makes them (`hosts.ts`, `start-run.ts`,
 * `run-control.ts`; the overview is the app's own), each group owning its channel names, the
 * bridge methods its view calls, and the parser the main process runs on what arrives. This module
 * composes them: `WELCOME_CHANNELS` and `WelcomeBridge` are the union of the groups, and
 * `parseWelcomeRequest` sends a channel to its group's parser.
 */
import type {HostKey, HostListing} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {
  HOSTS_CHANNELS,
  type HostsRequest,
  type HostsWelcomeBridge,
  parseHostsRequest,
} from './hosts.js';
import {
  parseRunControlRequest,
  RUN_CONTROL_CHANNELS,
  type RunControlRequest,
  type RunControlWelcomeBridge,
} from './run-control.js';
import {
  parseStartRunRequest,
  START_RUN_CHANNELS,
  type StartRunRequest,
  type StartRunWelcomeBridge,
} from './start-run.js';
import type {ChromeState, WelcomeEvent, WelcomeOverview, WelcomeResult} from './types.js';
import {isChannelOf} from './values.js';

/** The app's own requests: the overview the welcome view loads first. */
const APP_CHANNELS = {overview: 'vibesys:welcome:overview'} as const;

export const WELCOME_CHANNELS = {
  ...APP_CHANNELS,
  ...HOSTS_CHANNELS,
  ...START_RUN_CHANNELS,
  ...RUN_CONTROL_CHANNELS,
} as const;

/**
 * What the welcome preload exposes to the welcome view (`window.vibesysWelcome`): one function per
 * request channel, and one subscription per event. The main process validates every argument.
 */
export interface WelcomeBridge
  extends HostsWelcomeBridge,
    StartRunWelcomeBridge,
    RunControlWelcomeBridge {
  readonly platform: string;
  overview(): Promise<WelcomeResult<WelcomeOverview>>;
  onChrome(listener: (event: WelcomeEvent<ChromeState>) => void): void;
  onRecent(listener: (event: WelcomeEvent<readonly RecentRun[]>) => void): void;
  onShowHost(listener: (event: WelcomeEvent<HostKey>) => void): void;
  onListing(listener: (event: WelcomeEvent<HostListing>) => void): void;
}

/** One welcome request, validated: what the main process acts on. */
export type WelcomeRequest =
  | {readonly kind: 'overview'}
  | HostsRequest
  | StartRunRequest
  | RunControlRequest;

/**
 * The typed request on `channel` with `args` (as the welcome view's bridge sent them); throws an
 * `Error` naming the unknown channel or the first invalid value.
 */
export function parseWelcomeRequest(channel: string, args: readonly unknown[]): WelcomeRequest {
  if (isChannelOf(APP_CHANNELS, channel)) return {kind: 'overview'};
  if (isChannelOf(HOSTS_CHANNELS, channel)) return parseHostsRequest(channel, args);
  if (isChannelOf(START_RUN_CHANNELS, channel)) return parseStartRunRequest(channel, args);
  if (isChannelOf(RUN_CONTROL_CHANNELS, channel)) return parseRunControlRequest(channel, args);
  throw new Error(`${JSON.stringify(channel)} is not a welcome request`);
}

export type {HostsWelcomeBridge} from './hosts.js';
export type {RunControlWelcomeBridge} from './run-control.js';
export type {StartRunWelcomeBridge} from './start-run.js';
export {
  type ChromeState,
  type PathCompletion,
  type StartOutcome,
  settled,
  WELCOME_EVENTS,
  type WelcomeEvent,
  type WelcomeOverview,
  type WelcomeResult,
} from './types.js';
