/**
 * The run-control feature's pure core: the run commands that do not belong to one sheet (attach,
 * resume, stop, retry, switching between the run and the welcome view), the banner answering the
 * latest of them, and the title strip's selectors. Its effects reach the main process only through
 * `RunControlBridge`, which the app implements with the welcome bridge.
 */

import type {WelcomeBridge} from '@vibesys/ipc';

/** The welcome bridge's requests the run-control feature makes. */
export type RunControlBridge = Pick<
  WelcomeBridge,
  'attach' | 'resume' | 'stop' | 'retry' | 'showRun' | 'showWelcome'
>;

export {
  type RunCommand,
  WELCOME_INITIAL,
  type WelcomeAction,
  type WelcomeRequest,
  type WelcomeState,
  welcomeStep,
} from './run-control.js';
export {
  connectionText,
  type RunLink,
  runLink,
  type StripControls,
  stripControls,
} from './strip.js';
