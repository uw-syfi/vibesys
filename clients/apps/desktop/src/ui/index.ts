/**
 * React screens (`dom` runtime): the orchestrator window and the data it takes. They read plain
 * snapshots and call plain callbacks; the renderer entry binds the callbacks to the app's atoms.
 */
export {
  type AppCommands,
  type HostSnapshot,
  OrchestratorApp,
} from './orchestrator/app.js';
export type {StartRunRequest} from './orchestrator/start-draft.js';
