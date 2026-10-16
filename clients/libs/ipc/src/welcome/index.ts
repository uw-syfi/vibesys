/**
 * What the welcome view and the main process exchange: one `ipcRenderer.invoke` channel per
 * request, and the events the main process sends.
 *
 * The requests are grouped by the feature that makes them (`hosts.ts`, `start-run.ts`,
 * `run-control.ts`, `settings.ts`, `machines.ts`, `new-run.ts`; the overview is the app's own), each group owning its channel names, the
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
  MACHINES_CHANNELS,
  type MachinesRequest,
  type MachinesWelcomeBridge,
  parseMachinesRequest,
} from './machines.js';
import {
  NEW_RUN_CHANNELS,
  type NewRunRequest,
  type NewRunWelcomeBridge,
  parseNewRunRequest,
} from './new-run.js';
import {
  parseRunControlRequest,
  RUN_CONTROL_CHANNELS,
  type RunControlRequest,
  type RunControlWelcomeBridge,
} from './run-control.js';
import {
  type PreferencesView,
  parseSettingsRequest,
  SETTINGS_CHANNELS,
  type SettingsRequest,
  type SettingsWelcomeBridge,
} from './settings.js';
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
  ...SETTINGS_CHANNELS,
  ...MACHINES_CHANNELS,
  ...NEW_RUN_CHANNELS,
} as const;

type WelcomeChannel = (typeof WELCOME_CHANNELS)[keyof typeof WELCOME_CHANNELS];

/** The complete positional shape of every welcome request at the renderer boundary. */
const WELCOME_ARGUMENT_COUNTS = {
  [WELCOME_CHANNELS.overview]: 0,
  [WELCOME_CHANNELS.hosts]: 0,
  [WELCOME_CHANNELS.host]: 1,
  [WELCOME_CHANNELS.setCheckout]: 3,
  [WELCOME_CHANNELS.cancelCheckout]: 1,
  [WELCOME_CHANNELS.cancelInspection]: 1,
  [WELCOME_CHANNELS.signIn]: 1,
  [WELCOME_CHANNELS.complete]: 2,
  [WELCOME_CHANNELS.inspect]: 3,
  [WELCOME_CHANNELS.logs]: 3,
  [WELCOME_CHANNELS.releaseLogs]: 0,
  [WELCOME_CHANNELS.tasks]: 2,
  [WELCOME_CHANNELS.harnesses]: 1,
  [WELCOME_CHANNELS.start]: 6,
  [WELCOME_CHANNELS.cancelStart]: 1,
  [WELCOME_CHANNELS.attach]: 2,
  [WELCOME_CHANNELS.resume]: 3,
  [WELCOME_CHANNELS.showRun]: 0,
  [WELCOME_CHANNELS.showWelcome]: 0,
  [WELCOME_CHANNELS.retry]: 0,
  [WELCOME_CHANNELS.stop]: 3,
  [WELCOME_CHANNELS.stopConfirmed]: 3,
  [WELCOME_CHANNELS.releaseRun]: 0,
  [WELCOME_CHANNELS.preferences]: 0,
  [WELCOME_CHANNELS.setAppearance]: 1,
  [WELCOME_CHANNELS.setNotifications]: 1,
  [WELCOME_CHANNELS.archive]: 2,
  [WELCOME_CHANNELS.machines]: 0,
  [WELCOME_CHANNELS.machine]: 1,
  [WELCOME_CHANNELS.inspectCheckout]: 2,
  [WELCOME_CHANNELS.removeMachine]: 1,
  [WELCOME_CHANNELS.describeMachine]: 1,
  [WELCOME_CHANNELS.preflight]: 2,
  [WELCOME_CHANNELS.examples]: 1,
  [WELCOME_CHANNELS.createFromExample]: 3,
  [WELCOME_CHANNELS.copyOut]: 3,
  [WELCOME_CHANNELS.startRun]: 2,
} as const satisfies Readonly<Record<WelcomeChannel, number>>;

/**
 * What the welcome preload exposes to the welcome view (`window.vibesysWelcome`): one function per
 * request channel, and one subscription per event. The main process validates every argument.
 */
export interface WelcomeBridge
  extends HostsWelcomeBridge,
    StartRunWelcomeBridge,
    RunControlWelcomeBridge,
    SettingsWelcomeBridge,
    MachinesWelcomeBridge,
    NewRunWelcomeBridge {
  readonly platform: string;
  overview(): Promise<WelcomeResult<WelcomeOverview>>;
  onChrome(listener: (event: WelcomeEvent<ChromeState>) => void): void;
  onRecent(listener: (event: WelcomeEvent<readonly RecentRun[]>) => void): void;
  onShowHost(listener: (event: WelcomeEvent<HostKey>) => void): void;
  onListing(listener: (event: WelcomeEvent<HostListing>) => void): void;
  onPreferences(listener: (event: WelcomeEvent<PreferencesView>) => void): void;
}

/** One welcome request, validated: what the main process acts on. */
export type WelcomeRequest =
  | {readonly kind: 'overview'}
  | HostsRequest
  | StartRunRequest
  | RunControlRequest
  | SettingsRequest
  | MachinesRequest
  | NewRunRequest;

/**
 * The typed request on `channel` with `args` (as the welcome view's bridge sent them); throws an
 * `Error` naming the unknown channel or the first invalid value.
 */
export function parseWelcomeRequest(channel: string, args: readonly unknown[]): WelcomeRequest {
  if (!Object.hasOwn(WELCOME_ARGUMENT_COUNTS, channel)) {
    throw new Error(`${JSON.stringify(channel)} is not a welcome request`);
  }
  const expected = WELCOME_ARGUMENT_COUNTS[channel as WelcomeChannel];
  if (args.length !== expected) {
    const noun = expected === 1 ? 'argument' : 'arguments';
    throw new Error(`${channel} expects ${expected} ${noun}, not ${args.length}`);
  }
  if (isChannelOf(APP_CHANNELS, channel)) return {kind: 'overview'};
  if (isChannelOf(HOSTS_CHANNELS, channel)) return parseHostsRequest(channel, args);
  if (isChannelOf(START_RUN_CHANNELS, channel)) return parseStartRunRequest(channel, args);
  if (isChannelOf(RUN_CONTROL_CHANNELS, channel)) return parseRunControlRequest(channel, args);
  if (isChannelOf(SETTINGS_CHANNELS, channel)) return parseSettingsRequest(channel, args);
  if (isChannelOf(MACHINES_CHANNELS, channel)) return parseMachinesRequest(channel, args);
  if (isChannelOf(NEW_RUN_CHANNELS, channel)) return parseNewRunRequest(channel, args);
  // `WELCOME_ARGUMENT_COUNTS` is exhaustive over the same composed channel object.
  throw new Error(`${JSON.stringify(channel)} is not a welcome request`);
}

export type {HostsWelcomeBridge, WelcomeInspection} from './hosts.js';
export type {
  CheckoutInspection,
  MachineEntry,
  MachineReport,
  MachinesWelcomeBridge,
} from './machines.js';
export type {
  HarnessPick,
  NewRunWelcomeBridge,
  Offered,
  RoleOption,
  RunInputs,
  RunOptions,
  StartedRun,
} from './new-run.js';

export {RUN_PORT_MESSAGE, type RunControlWelcomeBridge} from './run-control.js';
export {
  FIRST_PAINT_CHANNEL,
  type FirstPaint,
  type PreferencesView,
  type SettingsWelcomeBridge,
} from './settings.js';
export type {
  HarnessChoice,
  HarnessListing,
  HarnessOffer,
  StartRunWelcomeBridge,
} from './start-run.js';
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
