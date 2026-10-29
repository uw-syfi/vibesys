import {unprovided} from '@vibesys/machine';
import type {ChatSessionCreated} from '@vibesys/runs/chat';
import {assign, fromCallback, setup} from 'xstate';
import {type ChatHistory, emptyHistory} from './fold.js';

export interface ThreadsContext {
  readonly sessions: readonly ChatSessionCreated[];
  /** Threads this client created: a listing that was already in flight cannot take them back. */
  readonly created: readonly ChatSessionCreated[];
  readonly histories: Readonly<Record<string, ChatHistory>>;
  readonly selected: string | null;
  readonly listError: string | null;
  readonly historyError: string | null;
}
export type ThreadsEvent =
  | {readonly type: 'reload'}
  | {readonly type: 'refresh'}
  | {readonly type: 'created'; readonly session: ChatSessionCreated}
  | {readonly type: 'select'; readonly id: string | null}
  | {
      readonly type: 'history';
      readonly id: string;
      readonly history: ChatHistory;
      readonly error: string | null;
    };

/** Every thread the run is known to have: the listing, then those created here it has not caught up with. */
export function knownSessions(context: ThreadsContext): readonly ChatSessionCreated[] {
  const listed = new Set(context.sessions.map(session => session.id));
  return [...context.sessions, ...context.created.filter(session => !listed.has(session.id))];
}

/** Metadata discovery and the selected session have independently owned lifetimes. */
export const threadsMachine = setup({
  types: {context: {} as ThreadsContext, events: {} as ThreadsEvent},
  actors: {
    list: unprovided<readonly ChatSessionCreated[]>('list'),
    watch: fromCallback<ThreadsEvent, {id: string; history: ChatHistory}>(() => {
      throw new Error('watch was not provided');
    }),
  },
  delays: {deadline: 15000},
}).createMachine({
  context: {
    sessions: [],
    created: [],
    histories: {},
    selected: null,
    listError: null,
    historyError: null,
  },
  type: 'parallel',
  on: {
    created: {
      actions: assign(({context, event}) => ({created: [...context.created, event.session]})),
    },
    history: {
      guard: ({context, event}) => event.id === context.selected,
      actions: assign(({context, event}) => ({
        histories: {...context.histories, [event.id]: event.history},
        historyError: event.error,
      })),
    },
  },
  states: {
    listing: {
      initial: 'loading',
      on: {refresh: {target: '.loading'}},
      states: {
        loading: {
          invoke: {
            id: 'list',
            src: 'list',
            onDone: {
              target: 'ready',
              actions: assign(({event}) => ({sessions: event.output, listError: null})),
            },
            onError: {
              target: 'failed',
              actions: assign(({event}) => ({listError: String(event.error)})),
            },
          },
          after: {
            deadline: {target: 'failed', actions: assign({listError: 'Thread list unavailable'})},
          },
        },
        ready: {},
        failed: {},
      },
    },
    viewing: {
      initial: 'idle',
      on: {
        reload: {
          guard: ({context}) => context.selected !== null,
          target: '.watching',
          actions: assign({historyError: null}),
        },
        select: [
          {guard: ({context, event}) => context.selected === event.id},
          {
            guard: ({event}) => event.id === null,
            target: '.idle',
            actions: assign({selected: null}),
          },
          {
            target: '.watching',
            actions: assign(({event}) => ({selected: event.id, historyError: null})),
          },
        ],
      },
      states: {
        idle: {},
        watching: {
          invoke: {
            id: 'watch',
            src: 'watch',
            input: ({context}) => {
              if (!context.selected) throw new Error('No selected thread');
              return {
                id: context.selected,
                history: context.histories[context.selected] ?? emptyHistory,
              };
            },
          },
        },
      },
    },
  },
});
