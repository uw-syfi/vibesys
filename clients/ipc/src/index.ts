/**
 * The typed messages between the desktop main process and its renderers, as data: the channel
 * names, the payload types, and the validation of what a renderer sends. The run page's bridge
 * (`bridge.ts`) carries protocol connections; the welcome view's (`welcome.ts`) carries its
 * requests and the main process's pushes. No Electron import: the preloads and the main process
 * each bind these to Electron themselves.
 */
export {
  BRIDGE_CREDIT_WINDOW_BYTES,
  CONNECT_CHANNEL,
  DESKTOP_PLATFORMS,
  type DesktopBridge,
  type DesktopPlatform,
  type DesktopPreload,
  type DesktopSocket,
  type DesktopSocketHandlers,
  type MainToPage,
  type PageToMain,
  parsePageMessage,
  WAKE_CHANNEL,
} from './bridge.js';
export {
  type ChromeState,
  type PathCompletion,
  type StartOutcome,
  settled,
  WELCOME_CHANNELS,
  WELCOME_EVENTS,
  type WelcomeBridge,
  type WelcomeEvent,
  type WelcomeOverview,
  type WelcomeResult,
} from './welcome.js';
