/**
 * The run-control machine: the window state the main process pushes (`chrome`, which the title
 * strip renders), the banner line, and the run commands that do not belong to one sheet: attach,
 * resume, stop, retry, and switching between the run and the welcome view.
 *
 * Control state, so a machine (docs/contributing/clients-architecture.md, "State"): `commanding`
 * invokes the latest command, and a new command re-enters it, which stops the earlier one. A stopped
 * command never reports, so the banner answers the latest command only and a slow attach cannot
 * overwrite what a later resume says. The command itself is the `command` operation the shell
 * provides (`renderer/effects`).
 */

import type {HostKey} from '@vibesys/hosts';
import type {ChromeState, WelcomeResult} from '@vibesys/ipc';
import {unprovided} from '@vibesys/machine';
import type {ResumeRunId} from '@vibesys/runs';
import {assign, type SnapshotFrom, setup} from 'xstate';

interface Banner {
  readonly text: string;
  readonly error: boolean;
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

const INITIAL_VIEW: WelcomeState = {
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

function settledBanner(result: WelcomeResult<null>): Banner | null {
  return result.ok ? null : {text: result.error, error: true};
}

export const runControlMachine = setup({
  types: {context: {} as Context, events: {} as WelcomeAction},
  actors: {command: unprovided<WelcomeResult<null>, RunCommand>('command')},
}).createMachine({
  id: 'runControl',
  context: {view: INITIAL_VIEW, command: null},
  initial: 'idle',
  on: {
    chrome: {
      actions: assign(({context, event}) => ({view: {...context.view, chrome: event.chrome}})),
    },
    problem: {
      target: '.idle',
      actions: assign(({context, event}) => ({
        view: {...context.view, banner: {text: event.text, error: true}},
      })),
    },
    command: {
      target: '.commanding',
      reenter: true,
      actions: assign(({context, event}) => {
        const text = progress(event.command);
        return {
          command: event.command,
          view: {
            ...context.view,
            banner: text === null ? context.view.banner : {text, error: false},
          },
        };
      }),
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
            view: {...context.view, banner: settledBanner(event.output)},
          })),
        },
        onError: {
          target: 'idle',
          actions: assign(({context, event}) => ({
            view: {...context.view, banner: {text: String(event.error), error: true}},
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
