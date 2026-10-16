/**
 * The typed messages between the desktop main process and its renderers, as data: the channel
 * names, the payload types, and the validation of what a renderer sends. The run connection bridge
 * (`bridge.ts`) carries protocol connections; the welcome view's (`welcome/`) carries its
 * requests, grouped by the feature that makes them, and the main process's pushes. No Electron
 * import: the preloads and the main process each bind these to Electron themselves.
 */

export {
  BRIDGE_CREDIT_WINDOW_BYTES,
  CONNECT_CHANNEL,
  type DesktopBridge,
  type DesktopSocket,
  type DesktopSocketHandlers,
  type MainToPage,
  type PageToMain,
  parsePageMessage,
  WAKE_CHANNEL,
} from './bridge.js';
export {openPortSocket, type PagePort} from './port-socket.js';
export {
  parseRunLogsRequest,
  type RunLogsReply,
  type RunLogsRequest,
} from './run-logs.js';
export {
  type CheckoutInspection,
  FIRST_PAINT_CHANNEL,
  type FirstPaint,
  type HarnessChoice,
  type HarnessListing,
  type HarnessOffer,
  type HarnessPick,
  HISTORY_PAGE_MAX,
  type HostsWelcomeBridge,
  type MachineEntry,
  type MachineReport,
  type MachinesWelcomeBridge,
  type NewRunWelcomeBridge,
  type Offered,
  type PathCompletion,
  type PreferencesView,
  type PreflightResult,
  parseWelcomeRequest,
  type RoleOption,
  RUN_PORT_MESSAGE,
  type RunControlWelcomeBridge,
  type RunHistoryPage,
  type RunInputs,
  type RunOptions,
  runOfWindow,
  runWindowUrl,
  type SettingsWelcomeBridge,
  type StartedRun,
  type StartRunWelcomeBridge,
  settled,
  WELCOME_CHANNELS,
  WELCOME_EVENTS,
  type WelcomeBridge,
  type WelcomeEvent,
  type WelcomeOverview,
  type WelcomeRequest,
  type WelcomeResult,
  type WindowsWelcomeBridge,
} from './welcome/index.js';
