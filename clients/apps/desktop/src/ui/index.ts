/**
 * React screens (`dom` runtime): the window (its rail, the runs and the settings) and the data it
 * takes. They read plain snapshots and call plain callbacks; the renderer entry binds the callbacks
 * to the app's atoms.
 */
export {
  type AppCommands,
  type HostSnapshot,
  OrchestratorApp,
} from './orchestrator/app.js';
export type {StartRunRequest} from './orchestrator/start-draft.js';
export {SettingsScreen} from './settings/settings-screen.js';
export {ConnectedShell} from './shell/connected.js';
export {AppShell} from './shell/shell.js';
