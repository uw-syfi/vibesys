/**
 * The chat panel's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"): closed or open, and while open two regions, the asking flow (idle, creating a thread,
 * asking it) and the harness catalogue (loading, then ready). Creating a thread and asking are
 * invoked operations the shell provides over the run session; leaving their state ends them, so
 * a late answer is never read. The transcript itself is not here: it is the run fold's, read by
 * `thread-view`.
 */
import type {HarnessChoice, HarnessListing} from '@vibesys/ipc';
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';
import {type Catalogue, catalogueOf, defaultChoice} from './picker.js';
import {EMPTY_DRAFT, type SteerDraft} from './steer.js';
import type {ThreadChoice} from './thread-view.js';

/** How a thread creation or an ask ended. */
export type ChatOutcome<T> =
  | ({readonly ok: true} & T)
  | {readonly ok: false; readonly message: string};

export interface AskInput {
  readonly requestId: string;
  readonly threadId: string;
  readonly text: string;
}

/** A steer the user wrote in the composer: a card the panel shows beside the proposals. */
export interface UserSteer {
  readonly id: string;
  readonly draft: SteerDraft;
}

export interface PanelContext {
  readonly choice: ThreadChoice;
  /** The pair the user picked for a new thread; null until they pick (see `effectiveHarness`). */
  readonly harness: HarnessChoice | null;
  /** The pairs a new thread starts on when none is picked, best first: the last thread's, the run's. */
  readonly preferred: readonly (HarnessChoice | null)[];
  readonly catalogue: Catalogue;
  readonly composer: string;
  readonly listOpen: boolean;
  readonly pickerOpen: boolean;
  /** The question sent and not yet answered; `threadId` is null while its thread is created. */
  readonly pending: PendingQuestion | null;
  readonly uncertain: PendingQuestion | null;
  readonly userSteers: readonly UserSteer[];
  readonly error: string | null;
}

interface PendingQuestion {
  readonly threadId: string | null;
  readonly text: string;
  readonly requestId: string;
}

export type PanelEvent =
  | {readonly type: 'open'}
  | {readonly type: 'close'}
  | {readonly type: 'toggle'}
  | {readonly type: 'choose-thread'; readonly id: string}
  | {readonly type: 'new-chat'}
  | {readonly type: 'set-harness'; readonly harness: HarnessChoice | null}
  | {readonly type: 'set-preferred'; readonly preferred: readonly (HarnessChoice | null)[]}
  | {readonly type: 'set-composer'; readonly text: string}
  /** Turns the draft in the composer into a steer card (an empty draft is an empty card). */
  | {readonly type: 'new-steer'}
  | {readonly type: 'toggle-list'}
  | {readonly type: 'toggle-picker'}
  /** `threadId`: the thread on screen, null for a new chat. */
  | {readonly type: 'submit'; readonly threadId: string | null};

export type PanelMachineEvent =
  | Exclude<PanelEvent, {readonly type: 'submit'}>
  | {
      readonly type: 'submit';
      readonly threadId: string | null;
      readonly requestId: string;
    };

function pendingQuestion(
  context: PanelContext,
  event: Extract<PanelMachineEvent, {type: 'submit'}>,
): PendingQuestion {
  const text = context.composer.trim();
  const previous = context.uncertain;
  return {
    threadId: event.threadId,
    text,
    requestId:
      previous?.threadId === event.threadId && previous.text === text
        ? previous.requestId
        : event.requestId,
  };
}

export const panelInitial: PanelContext = {
  choice: {kind: 'last'},
  harness: null,
  preferred: [],
  catalogue: {kind: 'loading'},
  composer: '',
  listOpen: false,
  pickerOpen: false,
  pending: null,
  uncertain: null,
  userSteers: [],
  error: null,
};

/** The patch for an ask or a thread creation that did not go through: the text is the user's again. */
function failed(context: PanelContext, message: string): Partial<PanelContext> {
  return {
    pending: null,
    uncertain: context.pending,
    composer: context.pending?.text ?? context.composer,
    error: message,
  };
}

function outcomeMessage(output: ChatOutcome<unknown>): string {
  return output.ok ? '' : output.message;
}

/** The pair a new thread is made with: the user's pick, else the best preferred pair offered. */
export function effectiveHarness(context: PanelContext): HarnessChoice | null {
  if (context.harness !== null) return context.harness;
  return context.catalogue.kind === 'listed'
    ? defaultChoice(context.catalogue.offers, context.preferred)
    : null;
}

export const panelMachine = setup({
  types: {context: {} as PanelContext, events: {} as PanelMachineEvent},
  actors: {
    loadCatalogue: unprovided<HarnessListing>('loadCatalogue'),
    createThread: unprovided<
      ChatOutcome<{readonly threadId: string}>,
      {harness: HarnessChoice | null; requestId: string}
    >('createThread'),
    ask: unprovided<ChatOutcome<unknown>, AskInput>('ask'),
  },
  guards: {
    hasThread: ({event}) => event.type === 'submit' && event.threadId !== null,
  },
}).createMachine({
  id: 'chatPanel',
  context: panelInitial,
  type: 'parallel',
  on: {
    'set-composer': {actions: assign(({event}) => ({composer: event.text}))},
    'new-steer': {
      actions: assign(({context}) => ({
        userSteers: [
          ...context.userSteers,
          {
            id: `user ${context.userSteers.length + 1}`,
            draft: {...EMPTY_DRAFT, text: context.composer.trim()},
          },
        ],
        composer: '',
      })),
    },
    'toggle-list': {actions: assign(({context}) => ({listOpen: !context.listOpen}))},
    'toggle-picker': {actions: assign(({context}) => ({pickerOpen: !context.pickerOpen}))},
    'set-preferred': {actions: assign(({event}) => ({preferred: event.preferred}))},
    'set-harness': {
      actions: assign(({event}) => ({harness: event.harness, pickerOpen: false, uncertain: null})),
    },
  },
  states: {
    visibility: {
      initial: 'closed',
      states: {
        closed: {on: {open: 'open', toggle: 'open'}},
        open: {on: {close: 'closed', toggle: 'closed'}},
      },
    },
    catalogue: {
      initial: 'idle',
      states: {
        idle: {on: {open: 'loading', toggle: 'loading'}},
        loading: {
          invoke: {
            id: 'loadCatalogue',
            src: 'loadCatalogue',
            onDone: {
              target: 'ready',
              actions: assign(({event}) => ({catalogue: catalogueOf(event.output)})),
            },
            onError: {target: 'ready', actions: assign({catalogue: {kind: 'none'}})},
          },
        },
        ready: {},
      },
    },
    chat: {
      initial: 'idle',
      /** Leaving a thread for another, or for a new chat, drops the question in flight; the panel stays. */
      on: {
        'choose-thread': {
          target: '.idle',
          actions: assign(({event}) => ({
            choice: {kind: 'thread', id: event.id},
            pending: null,
            uncertain: null,
            listOpen: false,
            pickerOpen: false,
          })),
        },
        'new-chat': {
          target: '.idle',
          actions: assign(() => ({
            choice: {kind: 'new'},
            pending: null,
            uncertain: null,
            composer: '',
            listOpen: false,
            pickerOpen: false,
          })),
        },
      },
      states: {
        idle: {
          on: {
            submit: [
              {
                guard: ({context}) => context.composer.trim() === '',
              },
              {
                guard: 'hasThread',
                target: 'asking',
                actions: assign(({context, event}) => ({
                  pending: pendingQuestion(context, event),
                  composer: '',
                  error: null,
                })),
              },
              {
                target: 'creating',
                actions: assign(({context, event}) => ({
                  pending: pendingQuestion(context, event),
                  composer: '',
                  error: null,
                })),
              },
            ],
          },
        },
        creating: {
          invoke: {
            id: 'createThread',
            src: 'createThread',
            input: ({context}) => {
              if (!context.pending) throw new Error('No pending chat');
              return {harness: effectiveHarness(context), requestId: context.pending.requestId};
            },
            onDone: [
              {
                guard: ({event}) => event.output.ok,
                target: 'asking',
                actions: assign(({context, event}) =>
                  event.output.ok
                    ? {
                        choice: {kind: 'thread' as const, id: event.output.threadId},
                        pending: {
                          threadId: event.output.threadId,
                          text: context.pending?.text ?? '',
                          requestId: context.pending?.requestId ?? '',
                        },
                      }
                    : {},
                ),
              },
              {
                target: 'idle',
                actions: assign(({context, event}) =>
                  failed(context, outcomeMessage(event.output)),
                ),
              },
            ],
            onError: {
              target: 'idle',
              actions: assign(({context, event}) => failed(context, String(event.error))),
            },
          },
        },
        asking: {
          invoke: {
            id: 'ask',
            src: 'ask',
            input: ({context}) => ({
              threadId: context.pending?.threadId ?? '',
              requestId: context.pending?.requestId ?? '',
              text: context.pending?.text ?? '',
            }),
            onDone: [
              {
                guard: ({event}) => event.output.ok,
                target: 'idle',
                actions: assign({pending: null, uncertain: null}),
              },
              {
                target: 'idle',
                actions: assign(({context, event}) =>
                  failed(context, outcomeMessage(event.output)),
                ),
              },
            ],
            onError: {
              target: 'idle',
              actions: assign(({context, event}) => failed(context, String(event.error))),
            },
          },
        },
      },
    },
  },
});
