/**
 * The start-run feature's pure core: the "Start a run" sheet's store (a project, its tasks, extra
 * arguments, and cancel while starting). The desktop main process reads arguments with the
 * reusable run-domain parser until this feature's `./main` entry exists.
 * Its effects reach the main process only through `StartRunBridge`, which the app implements with
 * the welcome bridge.
 */

import type {StartRunWelcomeBridge} from '@vibesys/ipc';

export {ShellWordsError, shellWords} from '@vibesys/runs';

/** The welcome bridge's requests the start-run feature makes. */
export type StartRunBridge = StartRunWelcomeBridge;

export {
  awaited,
  chosenHarness,
  type Harnesses,
  START_CLOSED,
  type StartAction,
  type StartRequest,
  type StartState,
  startable,
  startInvariants,
  startStep,
  type Tasks,
} from './start.js';
