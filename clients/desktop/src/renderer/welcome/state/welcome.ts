/**
 * The welcome screen's own store: the window state the main process pushes (`chrome`, which the
 * title strip renders), the banner line, and the run actions that do not belong to one sheet:
 * attach, resume, stop, retry, and switching between the run and the welcome view.
 *
 * The banner answers the latest action only: each action's request carries a token, and a reply
 * to an earlier action is dropped, so a slow attach cannot overwrite what a later resume says.
 */

import {only, type Step} from '@vibesys/core-state';
import type {HostKey, ResumeRunId} from '@vibesys/entities';
import type {ChromeState, WelcomeResult} from '@vibesys/ipc';

interface Banner {
  readonly text: string;
  readonly error: boolean;
}

export interface WelcomeState {
  readonly next: number;
  readonly chrome: ChromeState;
  readonly banner: Banner | null;
  /** The action whose reply the banner awaits. */
  readonly awaiting: number | null;
}

/** A request to the main process that changes what runs or what the window shows. */
export type RunCommand =
  | {readonly kind: 'attach'; readonly host: HostKey; readonly instance: string}
  | {
      readonly kind: 'resume';
      readonly host: HostKey;
      readonly project: string;
      readonly run: ResumeRunId;
    }
  | {
      readonly kind: 'stop';
      readonly host: HostKey;
      readonly instance: string;
      readonly force: boolean;
    }
  | {readonly kind: 'retry'}
  | {readonly kind: 'show-run'}
  | {readonly kind: 'show-welcome'};

export type WelcomeAction =
  | {readonly type: 'chrome'; readonly chrome: ChromeState}
  | {readonly type: 'problem'; readonly text: string}
  | {readonly type: 'command'; readonly command: RunCommand}
  | {readonly type: 'reply'; readonly token: number; readonly result: WelcomeResult<null>};

export type WelcomeRequest = {readonly token: number; readonly command: RunCommand};

export const WELCOME_INITIAL: WelcomeState = {
  next: 1,
  chrome: {mode: 'welcome', attached: null, stops: {}},
  banner: null,
  awaiting: null,
};

/** What the banner says while `command` runs; null for commands too quick to mention. */
function progress(command: RunCommand): string | null {
  switch (command.kind) {
    case 'attach':
      return `Attaching to ${command.instance}…`;
    case 'resume':
      return `Resuming ${command.run}…`;
    default:
      return null;
  }
}

export function welcomeStep(
  state: WelcomeState,
  action: WelcomeAction,
): Step<WelcomeState, WelcomeRequest> {
  switch (action.type) {
    case 'chrome':
      return only({...state, chrome: action.chrome});
    case 'problem':
      return only({...state, banner: {text: action.text, error: true}, awaiting: null});
    case 'command': {
      const token = state.next;
      const text = progress(action.command);
      return {
        state: {
          ...state,
          next: token + 1,
          awaiting: token,
          banner: text === null ? state.banner : {text, error: false},
        },
        requests: [{token, command: action.command}],
      };
    }
    case 'reply': {
      if (state.awaiting !== action.token) return only(state);
      const banner = action.result.ok ? null : {text: action.result.error, error: true};
      return only({...state, awaiting: null, banner});
    }
  }
}
