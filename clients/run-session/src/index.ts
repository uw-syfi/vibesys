/**
 * `@vibesys/run-session`: one live run, end to end below the UI. The run fold's wiring (`runStep`
 * over `@vibesys/run-state`), the session lifecycle (start-up, what the stream and the control
 * channel report, single-flight recovery), the ports the session needs from its platform, the run
 * connection over a control transport (`connectRun`), and `WebSession`, the shell that runs them.
 * Runtime-neutral: the app injects the transport and the page lifecycle.
 */
export {connectRun, type RunConnectionOptions} from './connect.js';
export type {WebSessionState, WebSessionStatus} from './connection.js';
export type {
  BrowserLifecycle,
  PageVisibility,
  RunConnection,
  RunStreamHandlers,
  TransportHooks,
} from './ports.js';
export {type RunAction, runStep} from './run.js';
export {createRunStore, type RunStore, WebSession, type WebSessionOptions} from './web-session.js';
