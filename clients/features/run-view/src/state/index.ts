// The run view's pure entry: the run fold, what the page knows about its connection, the banner
// copy, and the interfaces the live session needs from its platform.
export {
  CONTROLS_BANNER_COPY,
  connectionBanners,
  EMPTY_TRANSCRIPT_COPY,
  LIVE_SESSION_HEADER_COPY,
  PAGE_CONNECTION_BANNER_COPY,
  STREAM_BANNER_COPY,
} from './banners.js';
export type {WebSessionState, WebSessionStatus} from './connection.js';
export type {
  BrowserLifecycle,
  PageVisibility,
  RunConnection,
  RunStreamHandlers,
  TransportHooks,
} from './ports.js';
export {type RunAction, runStep} from './run.js';
