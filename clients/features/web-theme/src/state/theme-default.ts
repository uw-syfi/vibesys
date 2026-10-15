/**
 * The lifecycle for resolving a live host's Web theme default.
 *
 * A new session re-enters `resolving`, so XState owns cancellation and drops any late outcome
 * from the previous session. The app injects the I/O operation and observes only the current
 * default; theme preference data remains a separate pure fold.
 */

import {actorStore, unprovided} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import type {AbortSignalLike} from '@vibesys/transport';
import {assign, createActor, fromPromise, type SnapshotFrom, setup} from 'xstate';
import type {WebThemeName} from './palette.js';

export const DEFAULT_WEB_THEME: WebThemeName = 'dark';

/** The live-session boundary the app uses to resolve a host default after startup. */
export interface WebThemeSession {
  start(): Promise<void>;
  getState(): {readonly hasSnapshot: boolean};
  tuiDefaults(): Promise<{readonly theme: WebThemeName} | null>;
}

export interface HostThemeDefaultState {
  readonly theme: WebThemeName;
  readonly resolving: boolean;
}

export type HostThemeDefaultAction = {
  readonly type: 'resolve';
  readonly session: WebThemeSession;
};

export type ResolveHostTheme = (
  session: WebThemeSession,
  signal: AbortSignalLike,
) => Promise<WebThemeName>;

interface Context {
  readonly view: HostThemeDefaultState;
  readonly session: WebThemeSession | null;
}

const INITIAL_HOST_THEME: HostThemeDefaultState = {
  theme: DEFAULT_WEB_THEME,
  resolving: false,
};

export const hostThemeDefaultMachine = setup({
  types: {context: {} as Context, events: {} as HostThemeDefaultAction},
  actors: {
    resolve: unprovided<WebThemeName, WebThemeSession>('resolve host Web theme'),
  },
}).createMachine({
  id: 'hostThemeDefault',
  context: {view: INITIAL_HOST_THEME, session: null},
  initial: 'idle',
  on: {
    resolve: {
      target: '.resolving',
      reenter: true,
      actions: assign(({event}) => ({
        session: event.session,
        view: {theme: DEFAULT_WEB_THEME, resolving: true},
      })),
    },
  },
  states: {
    idle: {},
    resolving: {
      invoke: {
        id: 'resolve',
        src: 'resolve',
        input: ({context}) => context.session as WebThemeSession,
        onDone: {
          target: 'idle',
          actions: assign(({event}) => ({
            session: null,
            view: {theme: event.output, resolving: false},
          })),
        },
        onError: {
          target: 'idle',
          actions: assign({
            session: null,
            view: {theme: DEFAULT_WEB_THEME, resolving: false},
          }),
        },
      },
    },
  },
});

export function hostThemeDefaultView(
  snapshot: SnapshotFrom<typeof hostThemeDefaultMachine>,
): HostThemeDefaultState {
  return snapshot.context.view;
}

/** Create the long-lived app-shell store over the injected host-default operation. */
export function createHostThemeDefaultStore(
  resolve: ResolveHostTheme,
): Store<HostThemeDefaultState, HostThemeDefaultAction> {
  const machine = hostThemeDefaultMachine.provide({
    actors: {
      resolve: fromPromise<WebThemeName, WebThemeSession>(({input, signal}) =>
        resolve(input, signal),
      ),
    },
  });
  return actorStore(createActor(machine).start(), hostThemeDefaultView);
}
