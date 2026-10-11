/**
 * The start-run feature's pure core: the "Start a run" sheet's store (a project, its tasks, extra
 * arguments, and cancel while starting). Its effects reach the main process only through
 * `StartRunBridge`, which the app implements with the welcome bridge.
 */

import type {WelcomeBridge} from '@vibesys/ipc';

/** The welcome bridge's requests the start-run feature makes. */
export type StartRunBridge = Pick<WelcomeBridge, 'tasks' | 'start' | 'cancelStart'>;

export {
  awaited,
  START_CLOSED,
  type StartAction,
  type StartRequest,
  type StartState,
  startable,
  startInvariants,
  startStep,
  type Tasks,
} from './start.js';
