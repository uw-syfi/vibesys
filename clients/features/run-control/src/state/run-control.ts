/**
 * The run-control store: the window state the main process pushes (`chrome`, which the title
 * strip renders), the banner line, and the run actions that do not belong to one sheet:
 * attach, resume, stop, retry, and switching between the run and the welcome view.
 *
 * Command-owned banners answer only the latest action: each request carries a token, and a reply
 * to an earlier action is dropped, so a slow attach cannot overwrite what a later resume says.
 * An asynchronous problem is not owned by a command and stays visible through textless commands.
 */

import type {HostKey} from '@vibesys/hosts';
import type {ChromeState, WelcomeResult} from '@vibesys/ipc';
import type {ResumeRunId} from '@vibesys/runs';
import {only, type Step} from '@vibesys/store';

type BannerSource = 'progress' | 'problem' | 'reply';

interface Banner {
  readonly text: string;
  readonly error: boolean;
  /** Whether a command reply may replace this banner. */
  readonly source: BannerSource;
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
      return only({
        ...state,
        banner: {text: action.text, error: true, source: 'problem'},
        awaiting: null,
      });
    case 'command': {
      const token = state.next;
      const text = progress(action.command);
      return {
        state: {
          ...state,
          next: token + 1,
          awaiting: token,
          // Textless commands supersede only command-owned progress. An asynchronous problem
          // has no request token and must remain visible until an action actually replaces it.
          banner:
            text === null
              ? state.banner?.source === 'progress'
                ? null
                : state.banner
              : {text, error: false, source: 'progress'},
        },
        requests: [{token, command: action.command}],
      };
    }
    case 'reply': {
      if (state.awaiting !== action.token) return only(state);
      const banner: Banner | null = action.result.ok
        ? state.banner?.source === 'problem'
          ? state.banner
          : null
        : {text: action.result.error, error: true, source: 'reply'};
      return only({...state, awaiting: null, banner});
    }
  }
}
