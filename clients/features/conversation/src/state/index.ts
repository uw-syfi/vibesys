/** Run-scoped conversation state. Operations are provided by the renderer shell. */
import {unprovided} from '@vibesys/machine';
import {assign, type SnapshotFrom, setup} from 'xstate';

export type Intent = 'ask' | 'steer';
export interface Submission {
  readonly id: number;
  readonly intent: Intent;
  readonly text: string;
  readonly transcriptStart: number;
  readonly status: 'pending' | 'done' | 'error';
  readonly detail: string | null;
}
export interface Availability {
  readonly connected: boolean;
  readonly steerAvailable: boolean;
}
export interface ConversationState extends Availability {
  readonly open: boolean;
  readonly intent: Intent;
  readonly drafts: Readonly<Record<Intent, string>>;
  readonly submissions: readonly Submission[];
}
export type ConversationEvent =
  | {readonly type: 'open'; readonly open: boolean}
  | {readonly type: 'intent'; readonly intent: Intent}
  | {readonly type: 'draft'; readonly text: string}
  | {readonly type: 'availability'; readonly availability: Availability}
  | {readonly type: 'submit'; readonly transcriptStart: number};
interface Context {
  readonly view: ConversationState;
}

function canSend(view: ConversationState, intent: Intent): boolean {
  return (
    view.intent === intent &&
    view.connected &&
    (intent === 'ask' || view.steerAvailable) &&
    view.drafts[intent].trim().length > 0
  );
}
function pending(view: ConversationState, intent: Intent): Submission {
  const submission = view.submissions.find(
    item => item.intent === intent && item.status === 'pending',
  );
  if (submission === undefined) throw new Error(`Missing pending ${intent} submission`);
  return submission;
}
function finish(
  view: ConversationState,
  intent: Intent,
  status: 'done' | 'error',
  detail: string | null,
): ConversationState {
  return {
    ...view,
    submissions: view.submissions.map(item =>
      item.intent === intent && item.status === 'pending' ? {...item, status, detail} : item,
    ),
  };
}

export const conversationMachine = setup({
  types: {context: {} as Context, events: {} as ConversationEvent, input: {} as Availability},
  actors: {
    ask: unprovided<string | null, Submission>('ask'),
    steer: unprovided<string | null, Submission>('steer'),
  },
  guards: {
    canAsk: ({context}) => canSend(context.view, 'ask'),
    canSteer: ({context}) => canSend(context.view, 'steer'),
  },
  actions: {
    submitted: assign(({context, event}) => {
      if (event.type !== 'submit') return {};
      const view = context.view;
      return {
        view: {
          ...view,
          drafts: {...view.drafts, [view.intent]: ''},
          submissions: [
            ...view.submissions,
            {
              id: view.submissions.length + 1,
              intent: view.intent,
              text: view.drafts[view.intent].trim(),
              transcriptStart: event.transcriptStart,
              status: 'pending' as const,
              detail: null,
            },
          ],
        },
      };
    }),
  },
}).createMachine({
  id: 'conversation',
  context: ({input}) => ({
    view: {...input, open: false, intent: 'ask', drafts: {ask: '', steer: ''}, submissions: []},
  }),
  type: 'parallel',
  on: {
    open: {actions: assign(({context, event}) => ({view: {...context.view, open: event.open}}))},
    intent: {
      actions: assign(({context, event}) => ({view: {...context.view, intent: event.intent}})),
    },
    draft: {
      actions: assign(({context, event}) => ({
        view: {
          ...context.view,
          drafts: {...context.view.drafts, [context.view.intent]: event.text},
        },
      })),
    },
    availability: {
      actions: assign(({context, event}) => ({view: {...context.view, ...event.availability}})),
    },
  },
  states: {
    ask: {
      initial: 'idle',
      states: {
        idle: {on: {submit: {guard: 'canAsk', target: 'sending', actions: 'submitted'}}},
        sending: {
          invoke: {
            id: 'ask',
            src: 'ask',
            input: ({context}) => pending(context.view, 'ask'),
            onDone: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                view: finish(context.view, 'ask', 'done', event.output),
              })),
            },
            onError: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                view: finish(
                  context.view,
                  'ask',
                  'error',
                  `Chat failed: ${errorMessage(event.error)}`,
                ),
              })),
            },
          },
        },
      },
    },
    steer: {
      initial: 'idle',
      states: {
        idle: {on: {submit: {guard: 'canSteer', target: 'sending', actions: 'submitted'}}},
        sending: {
          invoke: {
            id: 'steer',
            src: 'steer',
            input: ({context}) => pending(context.view, 'steer'),
            onDone: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                view: finish(context.view, 'steer', 'done', event.output),
              })),
            },
            onError: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                view: finish(
                  context.view,
                  'steer',
                  'error',
                  `Steering failed: ${errorMessage(event.error)}`,
                ),
              })),
            },
          },
        },
      },
    },
  },
});

export function conversationView(
  snapshot: SnapshotFrom<typeof conversationMachine>,
): ConversationState {
  return snapshot.context.view;
}
function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
