/**
 * What the welcome view and the main process exchange: one `ipcRenderer.invoke` channel per
 * request, and the events the main process sends.
 *
 * The requests are grouped by the feature that makes them (`hosts.ts`, `start-run.ts`,
 * `run-control.ts`, `settings.ts`, `machines.ts`, `new-run.ts`, `windows.ts`; the overview is the app's own), each group owning its channel names, the
 * bridge methods its view calls, and the parser the main process runs on what arrives. This module
 * composes them: `WELCOME_CHANNELS` and `WelcomeBridge` are the union of the groups, and
 * `parseWelcomeRequest` sends a channel to its group's parser.
 */
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {
  HOSTS_CHANNELS,
  type HostsRequest,
  type HostsWelcomeBridge,
  parseHostsRequest,
} from './hosts.js';
import {
  MACHINE_SETTINGS_CHANNELS,
  type MachineSettingsRequest,
  type MachineSettingsWelcomeBridge,
  parseMachineSettingsRequest,
} from './machine-settings.js';
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
import type {WelcomeEvent, WelcomeOverview, WelcomeResult} from './types.js';
import {isChannelOf} from './values.js';
import {
  parseWindowsRequest,
  WINDOWS_CHANNELS,
  type WindowsRequest,
  type WindowsWelcomeBridge,
} from './windows.js';

/** The app's own requests: the overview the shell loads first. */
const APP_CHANNELS = {overview: 'vibesys:welcome:overview'} as const;

export const WELCOME_CHANNELS = {
  ...APP_CHANNELS,
  ...HOSTS_CHANNELS,
  ...START_RUN_CHANNELS,
  ...RUN_CONTROL_CHANNELS,
  ...SETTINGS_CHANNELS,
  ...MACHINES_CHANNELS,
  ...MACHINE_SETTINGS_CHANNELS,
  ...NEW_RUN_CHANNELS,
  ...WINDOWS_CHANNELS,
} as const;

type WelcomeChannel = (typeof WELCOME_CHANNELS)[keyof typeof WELCOME_CHANNELS];

/** The complete positional shape of every welcome request at the renderer boundary. */
const WELCOME_ARGUMENT_COUNTS = {
  [WELCOME_CHANNELS.overview]: 0,
  [WELCOME_CHANNELS.setCheckout]: 3,
  [WELCOME_CHANNELS.cancelCheckout]: 1,
  [WELCOME_CHANNELS.signIn]: 1,
  [WELCOME_CHANNELS.retryListing]: 1,
  [WELCOME_CHANNELS.complete]: 2,
  [WELCOME_CHANNELS.logs]: 3,
  [WELCOME_CHANNELS.releaseLogs]: 0,
  [WELCOME_CHANNELS.tasks]: 2,
  [WELCOME_CHANNELS.harnesses]: 1,
  [WELCOME_CHANNELS.attach]: 2,
  [WELCOME_CHANNELS.attachReader]: 3,
  [WELCOME_CHANNELS.resume]: 3,
  [WELCOME_CHANNELS.releaseRun]: 0,
  [WELCOME_CHANNELS.readEvents]: 5,
  [WELCOME_CHANNELS.readTrace]: 5,
  [WELCOME_CHANNELS.readSummary]: 3,
  [WELCOME_CHANNELS.readSpan]: 6,
  [WELCOME_CHANNELS.runDigests]: 3,
  [WELCOME_CHANNELS.projectRuns]: 3,
  [WELCOME_CHANNELS.cancelProjectRuns]: 1,
  [WELCOME_CHANNELS.usageWindow]: 5,
  [WELCOME_CHANNELS.usageSummary]: 4,
  [WELCOME_CHANNELS.usageRatesRefresh]: 3,
  [WELCOME_CHANNELS.usageLimits]: 3,
  [WELCOME_CHANNELS.usagePricesSave]: 4,
  [WELCOME_CHANNELS.preferences]: 0,
  [WELCOME_CHANNELS.setAppearance]: 1,
  [WELCOME_CHANNELS.setNotifications]: 1,
  [WELCOME_CHANNELS.archive]: 2,
  [WELCOME_CHANNELS.machines]: 0,
  [WELCOME_CHANNELS.machine]: 1,
  [WELCOME_CHANNELS.inspectCheckout]: 2,
  [WELCOME_CHANNELS.removeMachine]: 1,
  [WELCOME_CHANNELS.compute]: 1,
  [WELCOME_CHANNELS.testCluster]: 2,
  [WELCOME_CHANNELS.addCluster]: 2,
  [WELCOME_CHANNELS.removeCluster]: 2,
  [WELCOME_CHANNELS.agentLogins]: 1,
  [WELCOME_CHANNELS.startLogin]: 2,
  [WELCOME_CHANNELS.describeMachine]: 1,
  [WELCOME_CHANNELS.preflight]: 2,
  [WELCOME_CHANNELS.examples]: 1,
  [WELCOME_CHANNELS.createFromExample]: 3,
  [WELCOME_CHANNELS.copyOut]: 3,
  [WELCOME_CHANNELS.startRun]: 2,
  [WELCOME_CHANNELS.helperFeatures]: 1,
  [WELCOME_CHANNELS.taskGoal]: 3,
  [WELCOME_CHANNELS.exampleDestination]: 3,
  [WELCOME_CHANNELS.openRunWindow]: 1,
  [WELCOME_CHANNELS.showMainWindow]: 0,
  [WELCOME_CHANNELS.openSettings]: 0,
  [WELCOME_CHANNELS.openExternal]: 1,
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
    MachineSettingsWelcomeBridge,
    NewRunWelcomeBridge,
    WindowsWelcomeBridge {
  readonly platform: string;
  overview(): Promise<WelcomeResult<WelcomeOverview>>;
  onRecent(listener: (event: WelcomeEvent<readonly RecentRun[]>) => void): void;
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
  | MachineSettingsRequest
  | NewRunRequest
  | WindowsRequest;

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
  if (isChannelOf(MACHINE_SETTINGS_CHANNELS, channel)) {
    return parseMachineSettingsRequest(channel, args);
  }
  if (isChannelOf(NEW_RUN_CHANNELS, channel)) return parseNewRunRequest(channel, args);
  if (isChannelOf(WINDOWS_CHANNELS, channel)) return parseWindowsRequest(channel, args);
  // `WELCOME_ARGUMENT_COUNTS` is exhaustive over the same composed channel object.
  throw new Error(`${JSON.stringify(channel)} is not a welcome request`);
}

export type {HostsWelcomeBridge} from './hosts.js';
export type {
  AgentLogin,
  ClusterTest,
  ComputeSettings,
  LoginStart,
  MachineSettingsWelcomeBridge,
  SlurmClusterSettings,
} from './machine-settings.js';
export type {
  CheckoutInspection,
  MachineEntry,
  MachineReport,
  MachinesWelcomeBridge,
} from './machines.js';
export type {
  DestinationFound,
  ExampleCreation,
  HarnessPick,
  MeasureTarget,
  NewRunWelcomeBridge,
  Offered,
  PreflightResult,
  RoleOption,
  RunInputs,
  RunOptions,
  StartedRun,
} from './new-run.js';

export {
  HISTORY_PAGE_MAX,
  type ReaderAttached,
  RUN_PORT_MESSAGE,
  type RunControlWelcomeBridge,
  type RunHistoryPage,
  type RunSpanPage,
  type RunSummary,
  type RunSummaryResult,
  type SummaryTokens,
} from './run-control.js';
export {
  CHROME_INSET_CHANNEL,
  type ChromeInset,
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
  attempt,
  FAILURE_CAUSE_WORDS,
  type FailureCause,
  nextOperationToken,
  type PathCompletion,
  settled,
  UNEXPECTED_FAILURE,
  type UnexpectedFailure,
  WELCOME_EVENTS,
  type WelcomeEvent,
  type WelcomeOverview,
  type WelcomeResult,
} from './types.js';
export {runOfWindow, runWindowUrl, type WindowsWelcomeBridge} from './windows.js';
