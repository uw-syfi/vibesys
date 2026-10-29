/**
 * `@vibesys/run-session`: one live run, end to end below the UI. The run fold's wiring (`runStep`
 * over `@vibesys/run-state`), the session lifecycle as a machine (start-up, what the stream and the
 * control channel report, single-flight recovery), the ports the session needs from its platform, the run
 * connection over a control transport (`connectRun`), and `WebSession`, the shell that runs them.
 * Runtime-neutral: the app injects the transport and the page lifecycle.
 */
export {connectRun, type RunConnectionOptions} from './connect.js';
export type {WebSessionState, WebSessionStatus} from './connection.js';
export type {
  BrowserLifecycle,
  ChatHarness,
  ChatResponse,
  ChatResult,
  ChatThreadResponse,
  ChatThreadResult,
  IterationLoad,
  PageVisibility,
  RunCommand,
  RunCommandOutcome,
  RunConnection,
  RunStreamHandlers,
  TransportHooks,
  WebChatOptions,
} from './ports.js';
export {
  createRunStore,
  type IterationPublicationOutcome,
  type IterationRunMismatch,
  type RunAction,
  type RunDispatchOutcome,
  type RunStore,
  type RunTracePublication,
  runStep,
} from './run.js';
export {WebSession, type WebSessionOptions} from './web-session.js';
