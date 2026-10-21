/** Gateway screen control owns the setting, loader, content request and selected log follower. */
import {emptyLogView, foldChunk, type LogView} from '@vibesys/logs';
import {unprovided} from '@vibesys/machine';
import type {SkeletonLoad} from '@vibesys/run-skeleton';
import {assign, fromCallback, setup} from 'xstate';
import {
  type RunPageAction,
  type RunPageSeed,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from './run-page.js';

export interface GatewayInput {
  readonly seed: RunPageSeed;
}
export interface GatewayLogInput {
  readonly runId: string;
  readonly streamId: string;
  readonly receive: (event: GatewayEvent) => void;
}
export type GatewayEvent =
  | {type: 'toggle'; enabled: boolean}
  | {type: 'ui'; action: RunPageAction}
  | {type: 'log'; streamId: string; text: string; offset: number; next: number}
  | {type: 'log-failed'}
  | {type: 'loaded'; load: SkeletonLoad | undefined; failed: boolean};
export interface GatewayRunContext extends GatewayInput {
  enabled: boolean;
  load: SkeletonLoad | undefined;
  ui: RunPageState;
  text: Readonly<Record<string, string>>;
  contentHash: string;
  attemptedContent: ReadonlySet<string>;
  error: string | null;
}

function nextContentHash(context: GatewayRunContext): string {
  const epochs = context.load?.skeleton.epochs ?? [];
  for (let index = epochs.length - 1; index >= 0; index -= 1) {
    const hash = epochs[index]?.epoch?.run?.objective?.sha256 ?? '';
    if (hash !== '' && context.text[hash] === undefined && !context.attemptedContent.has(hash))
      return hash;
  }
  return '';
}

/** Presentation changes reuse the existing pure reducer; gateway commands never reach legacy ports. */
function uiStep(state: RunPageState, action: RunPageAction): RunPageState {
  switch (action.type) {
    case 'view':
    case 'ideas-filter':
    case 'usage-controls':
    case 'ideas-layout':
    case 'ideas-open':
    case 'ideas-pin':
    case 'ideas-hover':
    case 'agents-open':
    case 'select-stream':
    case 'view-logs':
      return runPageStep(state, action).state;
    default:
      return state;
  }
}

function receivedLog(
  context: GatewayRunContext,
  event: Extract<GatewayEvent, {type: 'log'}>,
): Readonly<Record<string, LogView>> {
  const previous =
    (Object.hasOwn(context.ui.views, event.streamId)
      ? context.ui.views[event.streamId]
      : undefined) ?? emptyLogView();
  return {
    ...context.ui.views,
    [event.streamId]: foldChunk(previous, {
      offset: event.offset,
      next_offset: event.next,
      data: event.text,
      truncated_before: false,
      eof: false,
      closed: false,
      pipes: [],
    }),
  };
}

export const gatewayRunMachine = setup({
  types: {context: {} as GatewayRunContext, input: {} as GatewayInput, events: {} as GatewayEvent},
  actors: {
    setting: unprovided<boolean, boolean | undefined>('transport setting'),
    loader: fromCallback<GatewayEvent, {runId: string}>(() => {
      throw new Error('loader not provided');
    }),
    content: unprovided<{hash: string; text: string}, {runId: string; hash: string}>(
      'content by hash',
    ),
    log: unprovided<void, GatewayLogInput>('log transport'),
  },
  guards: {
    needsContent: ({context}) => nextContentHash(context) !== '',
    hasStream: ({context}) => context.ui.selected !== null,
  },
}).createMachine({
  id: 'gatewayRun',
  context: ({input}) => ({
    ...input,
    enabled: false,
    load: undefined,
    ui: runPageInitial(input.seed),
    text: {},
    contentHash: '',
    attemptedContent: new Set(),
    error: null,
  }),
  initial: 'checking',
  on: {
    toggle: {
      target: '.saving',
      reenter: true,
      actions: assign({enabled: ({event}) => event.enabled, error: null}),
    },
  },
  states: {
    checking: {
      invoke: {
        id: 'read-setting',
        src: 'setting',
        input: undefined,
        onDone: [
          {guard: ({event}) => event.output, target: 'active', actions: assign({enabled: true})},
          {target: 'off', actions: assign({enabled: false})},
        ],
        onError: {target: 'failed', actions: assign({error: 'Unavailable'})},
      },
      after: {15000: {target: 'failed', actions: assign({error: 'Timeout'})}},
    },
    saving: {
      invoke: {
        id: 'save-setting',
        src: 'setting',
        input: ({context}) => context.enabled,
        onDone: [
          {guard: ({event}) => event.output, target: 'active', actions: assign({enabled: true})},
          {target: 'off'},
        ],
        onError: {target: 'failed', actions: assign({error: 'Unavailable'})},
      },
      after: {15000: {target: 'failed', actions: assign({error: 'Timeout'})}},
    },
    off: {},
    failed: {},
    active: {
      entry: assign({
        load: undefined,
        contentHash: '',
        attemptedContent: new Set(),
        ui: ({context}) => ({...context.ui, selected: null, views: {}}),
      }),
      type: 'parallel',
      invoke: {
        id: 'loader',
        src: 'loader',
        input: ({context}) => ({runId: context.seed.identity.runId}),
        onError: {actions: assign({error: 'Unavailable'})},
      },
      on: {
        loaded: {
          actions: assign({
            load: ({event}) => event.load,
            error: ({context, event}) => (event.failed ? 'Disconnected' : context.error),
          }),
        },
        ui: {actions: assign({ui: ({context, event}) => uiStep(context.ui, event.action)})},
      },
      states: {
        content: {
          initial: 'idle',
          states: {
            idle: {
              always: {
                guard: 'needsContent',
                target: 'reading',
                actions: assign(({context}) => {
                  const hash = nextContentHash(context);
                  return {
                    contentHash: hash,
                    attemptedContent: new Set([...context.attemptedContent, hash]),
                  };
                }),
              },
            },
            reading: {
              invoke: {
                id: 'content',
                src: 'content',
                input: ({context}) => ({
                  runId: context.seed.identity.runId,
                  hash: context.contentHash,
                }),
                onDone: {
                  target: 'idle',
                  actions: assign({
                    text: ({context, event}) => ({
                      ...context.text,
                      [event.output.hash]: event.output.text,
                    }),
                  }),
                },
                onError: {target: 'idle', actions: assign({error: 'Content unavailable'})},
              },
              after: {30000: {target: 'idle', actions: assign({error: 'Content timeout'})}},
            },
          },
        },
        logs: {
          initial: 'idle',
          on: {
            ui: [
              {
                guard: ({event, context}) =>
                  event.action.type === 'select-stream' &&
                  event.action.streamId !== context.ui.selected,
                target: '.following',
                actions: assign({ui: ({context, event}) => uiStep(context.ui, event.action)}),
              },
              {actions: assign({ui: ({context, event}) => uiStep(context.ui, event.action)})},
            ],
          },
          states: {
            idle: {},
            following: {
              initial: 'waiting',
              states: {
                waiting: {
                  after: {
                    30000: {
                      target: '#gatewayRun.active.logs.idle',
                      actions: assign({error: 'Log timeout'}),
                    },
                  },
                  on: {
                    log: {
                      target: 'live',
                      actions: assign({
                        ui: ({context, event}) => ({
                          ...context.ui,
                          views: receivedLog(context, event),
                        }),
                      }),
                    },
                  },
                },
                live: {},
              },
              invoke: {
                id: 'log',
                src: 'log',
                input: ({context, self}) => ({
                  runId: context.seed.identity.runId,
                  streamId: context.ui.selected ?? '',
                  receive: event => self.send(event),
                }),
                onError: {target: 'idle', actions: assign({error: 'Log unavailable'})},
              },
              on: {
                log: {
                  actions: assign({
                    ui: ({context, event}) => ({...context.ui, views: receivedLog(context, event)}),
                  }),
                },
                'log-failed': {target: 'idle', actions: assign({error: 'Log unavailable'})},
              },
            },
          },
        },
      },
    },
  },
});
