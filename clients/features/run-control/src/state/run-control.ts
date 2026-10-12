/**
 * The run-control machine: the window state the main process pushes (`chrome`, which the title
 * strip renders), the banner line, and the run commands that do not belong to one sheet: attach,
 * resume, stop, retry, and switching between the run and the welcome view.
 *
 * Control state, so a machine (docs/contributing/clients-architecture.md, "State"): `commanding`
 * invokes the latest command, and a new command re-enters it, which stops the earlier one. A stopped
 * command never reports, so the banner answers the latest command only and a slow attach cannot
 * overwrite what a later resume says. The command itself is the `command` operation the shell
 * provides (`renderer/effects`). An asynchronous problem is not owned by a command and stays
 * visible through textless commands and their successful replies.
 */

import type {HostKey} from '@vibesys/hosts';
import type {ChromeState, WelcomeResult} from '@vibesys/ipc';
import {unprovided} from '@vibesys/machine';
import type {ResumeRunId} from '@vibesys/runs';
import {assign, type SnapshotFrom, setup} from 'xstate';

type BannerSource = 'progress' | 'problem' | 'reply';

interface Banner {
  readonly text: string;
  readonly error: boolean;
  /** Whether a command reply may replace this banner. */
  readonly source: BannerSource;
}

/** What the title strip and the banner render. */
export interface WelcomeState {
  readonly chrome: ChromeState;
  readonly banner: Banner | null;
  /** Whether a command is in flight. */
  readonly busy: boolean;
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
  | {readonly type: 'command'; readonly command: RunCommand};

interface Context {
  readonly view: WelcomeState;
  /** The command `commanding` performs. */
  readonly command: RunCommand | null;
}

/** What the welcome view shows before any command: the machine's initial view. */
export const WELCOME_INITIAL: WelcomeState = {
  chrome: {mode: 'welcome', attached: null, stops: {}},
  banner: null,
  busy: false,
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

/**
 * The banner once the latest command settles: a failure replaces it, and a success clears
 * command-owned text but keeps an asynchronous problem, which no command owns.
 */
function settledBanner(current: Banner | null, result: WelcomeResult<null>): Banner | null {
  if (!result.ok) return {text: result.error, error: true, source: 'reply'};
  return current?.source === 'problem' ? current : null;
}

/**
 * The banner while `command` runs. Textless commands supersede only command-owned progress; an
 * asynchronous problem has no command and must remain visible until an action replaces it.
 */
function commandBanner(current: Banner | null, command: RunCommand): Banner | null {
  const text = progress(command);
  if (text !== null) return {text, error: false, source: 'progress'};
  return current?.source === 'progress' ? null : current;
}

export const runControlMachine = setup({
  types: {context: {} as Context, events: {} as WelcomeAction},
  actors: {command: unprovided<WelcomeResult<null>, RunCommand>('command')},
}).createMachine({
  id: 'runControl',
  context: {view: WELCOME_INITIAL, command: null},
  initial: 'idle',
  on: {
    chrome: {
      actions: assign(({context, event}) => ({view: {...context.view, chrome: event.chrome}})),
    },
    problem: {
      target: '.idle',
      actions: assign(({context, event}) => ({
        view: {...context.view, banner: {text: event.text, error: true, source: 'problem'}},
      })),
    },
    command: {
      target: '.commanding',
      reenter: true,
      actions: assign(({context, event}) => ({
        command: event.command,
        view: {...context.view, banner: commandBanner(context.view.banner, event.command)},
      })),
    },
  },
  states: {
    idle: {
      entry: assign(({context}) =>
        context.view.busy ? {view: {...context.view, busy: false}, command: null} : {},
      ),
    },
    commanding: {
      entry: assign(({context}) =>
        context.view.busy ? {} : {view: {...context.view, busy: true}},
      ),
      invoke: {
        id: 'command',
        src: 'command',
        input: ({context}) => context.command as RunCommand,
        onDone: {
          target: 'idle',
          actions: assign(({context, event}) => ({
            view: {...context.view, banner: settledBanner(context.view.banner, event.output)},
          })),
        },
        onError: {
          target: 'idle',
          actions: assign(({context, event}) => ({
            view: {
              ...context.view,
              banner: {text: String(event.error), error: true, source: 'reply'},
            },
          })),
        },
      },
    },
  },
});

/** What the view renders of a run-control snapshot. */
export function welcomeView(snapshot: SnapshotFrom<typeof runControlMachine>): WelcomeState {
  return snapshot.context.view;
}
