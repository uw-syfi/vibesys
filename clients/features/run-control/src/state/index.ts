/**
 * The run-control feature's pure core: the machine for the run commands that do not belong to one
 * sheet (attach, resume, stop, retry, switching between the run and the welcome view), the banner
 * answering the latest of them, and the title strip's selectors. Its effects reach the main process only through
 * `RunControlBridge`, which the app implements with the welcome bridge.
 */

import type {RunControlWelcomeBridge} from '@vibesys/ipc';

/** The welcome bridge's requests the run-control feature makes. */
export type RunControlBridge = RunControlWelcomeBridge;

export {
  type RunCommand,
  runControlMachine,
  WELCOME_INITIAL,
  type WelcomeAction,
  type WelcomeState,
  welcomeView,
} from './run-control.js';
export {
  connectionText,
  type RunLink,
  runLink,
  type StripControls,
  stripControls,
} from './strip.js';
