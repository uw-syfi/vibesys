/**
 * One steer card as a machine (docs/contributing/clients-architecture.md, "State"): the editable
 * draft, the send, and what happens to it afterwards. Nothing leaves the card until `send`; the
 * send is an invoked operation (`deliver`, provided by the shell over `command.control`) whose
 * outcome is the command's acknowledgment. What the server then reports about delivery is a fold
 * (`foldDelivery`) the machine holds in its context; the machine itself only decides which of its
 * modes accepts which event:
 *
 *   draft -> sending -> sent (idle | withdrawing | withdrawRequested), or back to draft with the
 *   refusal; draft -> dismissed.
 *
 * Delivery events carry the control id they report on. Events for another control are ignored,
 * and events that arrive before the acknowledgment are kept (the fold is order-free).
 */
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';
import {
  type DeliveryEvent,
  type DeliveryState,
  deliveryEnded,
  foldDelivery,
  SENT,
} from './delivery.js';
import {type ChatFailure, thrownFailure} from './failure.js';
import {
  draftProblem,
  type SteerDraft,
  type SteerMode,
  type SteerRequest,
  type SteerTarget,
  steerRequest,
  type WithdrawRequest,
  withdrawRequest,
} from './steer.js';

/** The acknowledgment of one control command, as the shell reports it. */
export type ControlOutcome =
  | {readonly kind: 'acknowledged'}
  | {
      readonly kind: 'rejected';
      readonly message: string | null;
      readonly detail: string | null;
      readonly code?: string | null;
    };

/** The diagnostic code of a server that refuses every command, as it serves a run that ended. */
const READ_ONLY_CODE = 'run_read_only';

/** What a refusal says: the client's word for a typed reason it knows, else the server's message. */
function refusalFailure(output: ControlOutcome): ChatFailure | null {
  if (output.kind !== 'rejected') return null;
  const message = output.code === READ_ONLY_CODE ? 'Read only' : (output.message ?? 'Refused');
  return {message, detail: output.detail};
}

export type DeliverRequest = SteerRequest | WithdrawRequest;

export interface SteerCardContext {
  readonly id: string;
  /** A steer the agent proposed, or one the user wrote. */
  readonly origin: 'proposal' | 'user';
  readonly draft: SteerDraft;
  /** The control id the steer was sent under; null until it is sent. */
  readonly controlId: string | null;
  /** The control id of a withdrawal in flight or acknowledged; null otherwise. */
  readonly withdrawId: string | null;
  readonly delivery: DeliveryState;
  /** The run cannot take a steer (it ended): the draft is kept, Send is off. */
  readonly readOnly: boolean;
  /** Why the last send or withdrawal was not taken; null when nothing failed. */
  readonly error: ChatFailure | null;
}

export interface SteerCardInput {
  readonly id: string;
  readonly origin: 'proposal' | 'user';
  readonly draft: SteerDraft;
  readonly readOnly?: boolean;
}

export type SteerCardEvent =
  | {readonly type: 'read-only'; readonly value: boolean}
  | {readonly type: 'edit-text'; readonly text: string}
  | {readonly type: 'set-target'; readonly target: SteerTarget}
  | {readonly type: 'set-mode'; readonly mode: SteerMode}
  | {readonly type: 'send'; readonly controlId: string}
  | {readonly type: 'dismiss'}
  | {readonly type: 'withdraw'; readonly controlId: string}
  | {readonly type: 'delivery'; readonly controlId: string; readonly event: DeliveryEvent};

/** A delivery event is for this card when its control id is the steer's or its withdrawal's. */
function reportsOn(context: SteerCardContext, controlId: string): boolean {
  return controlId === context.controlId || controlId === context.withdrawId;
}

/** Whether a standing steer is in force, so Withdraw is offered. */
export function inForce(context: SteerCardContext): boolean {
  return context.draft.mode === 'standing' && context.delivery.kind === 'delivered';
}

export const steerCardMachine = setup({
  types: {
    context: {} as SteerCardContext,
    events: {} as SteerCardEvent,
    input: {} as SteerCardInput,
  },
  actors: {deliver: unprovided<ControlOutcome, {readonly request: DeliverRequest}>('deliver')},
  actions: {
    foldDelivery: assign(({context, event}) =>
      event.type === 'delivery' && reportsOn(context, event.controlId)
        ? {delivery: foldDelivery(context.delivery, event.event)}
        : {},
    ),
  },
  guards: {
    sendable: ({context}) => !context.readOnly && draftProblem(context.draft) === null,
    canWithdraw: ({context}) => inForce(context),
    refusesWithdrawal: ({context, event}) =>
      event.type === 'delivery' &&
      event.event.kind === 'refused' &&
      event.controlId === context.withdrawId &&
      !deliveryEnded(context.delivery),
    acknowledged: (_, output: ControlOutcome) => output.kind === 'acknowledged',
  },
}).createMachine({
  id: 'steerCard',
  context: ({input}) => ({
    id: input.id,
    origin: input.origin,
    draft: input.draft,
    controlId: null,
    withdrawId: null,
    delivery: SENT,
    readOnly: input.readOnly ?? false,
    error: null,
  }),
  on: {'read-only': {actions: assign(({event}) => ({readOnly: event.value}))}},
  initial: 'draft',
  states: {
    draft: {
      on: {
        'edit-text': {
          actions: assign(({context, event}) => ({draft: {...context.draft, text: event.text}})),
        },
        'set-target': {
          actions: assign(({context, event}) => ({
            draft: {...context.draft, target: event.target},
          })),
        },
        'set-mode': {
          actions: assign(({context, event}) => ({draft: {...context.draft, mode: event.mode}})),
        },
        send: {
          guard: 'sendable',
          target: 'sending',
          actions: assign(({event}) => ({controlId: event.controlId, error: null})),
        },
        dismiss: 'dismissed',
      },
    },
    sending: {
      invoke: {
        id: 'send',
        src: 'deliver',
        input: ({context}) => ({
          request: steerRequest(context.controlId ?? '', context.draft),
        }),
        onDone: [
          {guard: {type: 'acknowledged', params: ({event}) => event.output}, target: 'sent'},
          {
            target: 'draft',
            actions: assign(({event}) => ({
              controlId: null,
              delivery: SENT,
              error: refusalFailure(event.output),
            })),
          },
        ],
        onError: {
          target: 'draft',
          actions: assign(({event}) => ({
            controlId: null,
            delivery: SENT,
            error: thrownFailure(event.error),
          })),
        },
      },
      // The server may report on the steer before its acknowledgment reaches the card.
      on: {delivery: {actions: 'foldDelivery'}},
    },
    sent: {
      initial: 'idle',
      on: {delivery: {actions: 'foldDelivery'}},
      states: {
        idle: {
          on: {
            withdraw: {
              guard: 'canWithdraw',
              target: 'withdrawing',
              actions: assign(({event}) => ({withdrawId: event.controlId, error: null})),
            },
          },
        },
        withdrawing: {
          invoke: {
            id: 'retract',
            src: 'deliver',
            input: ({context}) => ({
              request: withdrawRequest(context.withdrawId ?? '', context.controlId ?? ''),
            }),
            onDone: [
              {
                guard: {type: 'acknowledged', params: ({event}) => event.output},
                target: 'withdrawRequested',
              },
              {
                target: 'idle',
                actions: assign(({event}) => ({
                  withdrawId: null,
                  error:
                    event.output.kind === 'rejected'
                      ? {message: event.output.message ?? 'Refused', detail: event.output.detail}
                      : null,
                })),
              },
            ],
            onError: {
              target: 'idle',
              actions: assign(({event}) => ({withdrawId: null, error: thrownFailure(event.error)})),
            },
          },
        },
        withdrawRequested: {
          on: {
            delivery: [
              {
                guard: 'refusesWithdrawal',
                target: 'idle',
                actions: assign(({event}) => ({
                  withdrawId: null,
                  error:
                    event.type === 'delivery' && event.event.kind === 'refused'
                      ? {message: event.event.reason ?? 'Refused', detail: null}
                      : null,
                })),
              },
              {actions: 'foldDelivery'},
            ],
          },
        },
      },
    },
    dismissed: {type: 'final'},
  },
});
