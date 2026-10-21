import {unprovided} from '@vibesys/machine';
import type {ChatMessageRequest, ChatMessageResponse, Record} from '@vibesys/runs/chat';
import {copyChatRequest} from '@vibesys/runs/chat';
import {assign, fromCallback, setup} from 'xstate';
import {type ChatBatch, type ChatHistory, emptyHistory, foldChat} from './fold.js';

export interface ChatInput {
  readonly enabled: boolean;
}
export interface ChatContext {
  readonly enabled: boolean;
  readonly history: ChatHistory;
  readonly queue: readonly (readonly Record[])[];
  readonly current: ChatMessageRequest | null;
  readonly error: string | null;
  readonly uncertain: boolean;
  readonly contentFailed: boolean;
}
export type ChatEvent =
  | {readonly type: 'send'; readonly request: ChatMessageRequest}
  | {readonly type: 'toggle'}
  | {readonly type: 'retry'}
  | {readonly type: 'records'; readonly records: readonly Record[]}
  | {readonly type: 'disconnected'};
export const runSessionsMachine = setup({
  types: {context: {} as ChatContext, events: {} as ChatEvent, input: {} as ChatInput},
  actors: {
    post: unprovided<ChatMessageResponse, ChatMessageRequest>('post'),
    resolve: unprovided<ChatBatch, readonly Record[]>('resolve'),
    watch: fromCallback<ChatEvent, bigint>(() => {
      throw new Error('watch was not provided');
    }),
  },
  delays: {deadline: 35_000, reconnect: 1000},
}).createMachine({
  id: 'runSessions',
  context: ({input}) => ({
    enabled: input.enabled,
    history: emptyHistory,
    queue: [],
    current: null,
    error: null,
    uncertain: false,
    contentFailed: false,
  }),
  initial: 'routing',
  states: {
    routing: {
      always: [{guard: ({context}) => context.enabled, target: 'closed'}, {target: 'disabled'}],
    },
    disabled: {},
    closed: {
      on: {toggle: {target: 'active', actions: assign({contentFailed: false, error: null})}},
    },
    active: {
      type: 'parallel',
      on: {
        toggle: {
          target: 'closed',
          actions: assign(({context}) => ({
            queue: [],
            uncertain: context.current !== null || context.uncertain,
          })),
        },
        records: [
          {guard: ({context}) => context.contentFailed},
          {
            guard: ({context}) => context.queue.length < 64,
            actions: assign(({context, event}) => ({queue: [...context.queue, event.records]})),
          },
          {
            target: '.content.failed',
            actions: assign({contentFailed: true, queue: [], error: 'History unavailable'}),
          },
        ],
      },
      states: {
        connection: {
          initial: 'watching',
          states: {
            watching: {
              invoke: {
                id: 'watch',
                src: 'watch',
                input: ({context}) => context.history.throughSeq + 1n,
                onError: {target: 'waiting'},
              },
              on: {disconnected: 'waiting'},
            },
            waiting: {after: {reconnect: 'watching'}},
          },
        },
        sending: {
          initial: 'idle',
          states: {
            idle: {
              on: {
                send: {
                  target: 'posting',
                  actions: assign(({event}) => ({
                    current: copyChatRequest(event.request),
                    uncertain: false,
                    error: null,
                  })),
                },
                retry: {
                  guard: ({context}) => context.uncertain && context.current !== null,
                  target: 'posting',
                  actions: assign({error: null}),
                },
              },
            },
            posting: {
              invoke: {
                id: 'post',
                src: 'post',
                input: ({context}) => {
                  if (!context.current) throw new Error('No chat message');
                  return context.current;
                },
                onDone: {target: 'idle', actions: assign({uncertain: false, current: null})},
                onError: {
                  target: 'idle',
                  actions: assign(({event}) => ({error: String(event.error), uncertain: true})),
                },
              },
              after: {
                deadline: {
                  target: 'idle',
                  actions: assign({error: 'Not confirmed', uncertain: true}),
                },
              },
            },
          },
        },
        content: {
          initial: 'idle',
          states: {
            idle: {always: {guard: ({context}) => context.queue.length > 0, target: 'loading'}},
            loading: {
              invoke: {
                id: 'resolve',
                src: 'resolve',
                input: ({context}) => context.queue[0] ?? [],
                onDone: [
                  {
                    guard: ({context, event}) => {
                      try {
                        foldChat(context.history, event.output);
                        return true;
                      } catch {
                        return false;
                      }
                    },
                    target: 'idle',
                    actions: assign(({context, event}) => ({
                      history: foldChat(context.history, event.output),
                      queue: context.queue.slice(1),
                    })),
                  },
                  {
                    target: 'failed',
                    actions: assign({contentFailed: true, queue: [], error: 'History unavailable'}),
                  },
                ],
                onError: {
                  target: 'failed',
                  actions: assign(({event}) => ({
                    contentFailed: true,
                    queue: [],
                    error: String(event.error),
                  })),
                },
              },
              after: {
                deadline: {
                  target: 'failed',
                  actions: assign({contentFailed: true, queue: [], error: 'Unavailable'}),
                },
              },
            },
            failed: {},
          },
        },
      },
    },
  },
});
