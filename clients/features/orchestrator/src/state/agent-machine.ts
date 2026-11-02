/**
 * Control state for one open agent, as a child of the host machine: its watch (dialled on
 * entering, closed on leaving or when the parent stops it), its older-history requests, and its
 * message box with the sends queued behind the one in flight. Nothing here is shared with another
 * agent, so one agent's fault, sequence, or message cannot show on another's screen.
 *
 * Data does not live here: every frame and page goes up to the parent as `agent.fold`, which
 * applies it to the host's state with `@vibesys/orchestrator-state`'s reducer.
 */
import {unprovided} from '@vibesys/machine';
import {
  type Command,
  type ItemsPage,
  type Receipt,
  sendMessageCommand,
  type WatchAgentResponse,
} from '@vibesys/orchestrator-proto';
import type {HostPorts} from '@vibesys/orchestrator-source';
import type {AgentFrame} from '@vibesys/orchestrator-state';
import {assign, fromCallback, sendParent, setup} from 'xstate';
import {
  CONNECT_DEADLINE_MS,
  type Fault,
  faultOf,
  type NewBackoff,
  REQUEST_DEADLINE_MS,
  redialDelay,
  STABLE_AFTER_MS,
  timeout,
} from './faults.js';

/** A message the user asked to send. Its id is chosen once and kept for every retry. */
export interface Outgoing {
  readonly commandId: string;
  readonly agentId: string;
  readonly text: string;
}

/** Where one agent's message box stands. */
export type MessageBox =
  | {readonly status: 'idle'}
  | {readonly status: 'sending'; readonly commandId: string}
  | {readonly status: 'answered'; readonly commandId: string; readonly receipt: Receipt}
  | {readonly status: 'failed'; readonly commandId: string; readonly fault: Fault};

export const idleBox: MessageBox = {status: 'idle'};

export interface AgentContext {
  readonly agentId: string;
  readonly ports: HostPorts;
  readonly backoff: NewBackoff;
  /** The newest sequence this agent's stream carried: where a redial continues from. */
  readonly sequence: bigint;
  readonly attempts: number;
  readonly fault: Fault | null;
  readonly olderError: Fault | null;
  readonly box: MessageBox;
  /** The send in flight, and those waiting behind it. */
  readonly inflight: Outgoing | null;
  readonly queue: readonly Outgoing[];
}

export type AgentEvent =
  | {readonly type: 'stream.frame'; readonly frame: WatchAgentResponse}
  | {readonly type: 'stream.over'; readonly fault: Fault}
  | {readonly type: 'retry'}
  | {readonly type: 'older'; readonly before: number}
  | ({readonly type: 'message.send'} & Outgoing)
  | {readonly type: 'message.dismiss'};

/** What a child tells the host machine: a frame or page to fold into the host's state. */
export interface AgentFold {
  readonly type: 'agent.fold';
  readonly frame: AgentFrame;
}

export interface AgentInput {
  readonly agentId: string;
  readonly ports: HostPorts;
  readonly backoff: NewBackoff;
}

/** What the stream actor needs to dial. */
export interface AgentStreamInput {
  readonly ports: HostPorts;
  readonly agentId: string;
  readonly after: bigint;
}

const sending = (commandId: string): MessageBox => ({status: 'sending', commandId});

export const agentMachine = setup({
  types: {
    context: {} as AgentContext,
    events: {} as AgentEvent,
    input: {} as AgentInput,
  },
  actors: {
    stream: fromCallback<AgentEvent, AgentStreamInput>(() => {
      throw new Error('stream was not provided');
    }),
    loadOlder: unprovided<ItemsPage, {ports: HostPorts; agentId: string; before: number}>(
      'loadOlder',
    ),
    sendMessage: unprovided<Receipt, {ports: HostPorts; command: Command}>('sendMessage'),
  },
  actions: {
    fold: sendParent(({event}): AgentFold => {
      if (event.type !== 'stream.frame') throw new Error('fold runs on a stream frame');
      return {type: 'agent.fold', frame: event.frame};
    }),
    remember: assign(({context, event}) =>
      event.type === 'stream.frame' && event.frame.sequence > context.sequence
        ? {sequence: event.frame.sequence}
        : {},
    ),
    live: assign({fault: null}),
    stable: assign({attempts: 0}),
    lost: assign(({event}) => ({fault: event.type === 'stream.over' ? event.fault : null})),
    timedOut: assign({fault: timeout('The host')}),
    redialed: assign(({context}) => ({attempts: context.attempts + 1})),
    retried: assign({attempts: 0, fault: null}),
    olderTimedOut: assign({olderError: timeout('The host')}),
    startSend: assign(({event}) =>
      event.type === 'message.send' ? {inflight: event, box: sending(event.commandId)} : {},
    ),
    enqueue: assign(({context, event}) =>
      event.type === 'message.send'
        ? {queue: [...context.queue, event], box: sending(event.commandId)}
        : {},
    ),
    dequeue: assign(({context}) => ({
      inflight: context.queue[0] ?? null,
      queue: context.queue.slice(1),
    })),
    dismiss: assign({box: idleBox}),
  },
  guards: {
    canRedial: ({context}) => redialDelay(context.backoff, context.attempts) !== undefined,
    hasQueued: ({context}) => context.queue.length > 0,
    notPending: ({context, event}) =>
      event.type === 'message.send' &&
      context.inflight?.commandId !== event.commandId &&
      !context.queue.some(waiting => waiting.commandId === event.commandId),
  },
  delays: {
    redial: ({context}) => redialDelay(context.backoff, context.attempts) ?? 0,
    connectDeadline: CONNECT_DEADLINE_MS,
    requestDeadline: REQUEST_DEADLINE_MS,
    stableAfter: STABLE_AFTER_MS,
  },
}).createMachine({
  id: 'orchestrator-agent',
  type: 'parallel',
  context: ({input}) => ({
    agentId: input.agentId,
    ports: input.ports,
    backoff: input.backoff,
    sequence: 0n,
    attempts: 0,
    fault: null,
    olderError: null,
    box: idleBox,
    inflight: null,
    queue: [],
  }),
  states: {
    stream: {
      initial: 'open',
      states: {
        // The watch is dialled on entering and closed on leaving, however the state is left.
        open: {
          invoke: {
            id: 'stream',
            src: 'stream',
            input: ({context}) => ({
              ports: context.ports,
              agentId: context.agentId,
              after: context.sequence,
            }),
          },
          initial: 'connecting',
          states: {
            connecting: {
              after: {
                connectDeadline: [
                  {guard: 'canRedial', target: '#agentWaiting', actions: 'timedOut'},
                  {target: '#agentGaveUp', actions: 'timedOut'},
                ],
              },
              on: {'stream.frame': {target: 'live', actions: ['fold', 'remember', 'live']}},
            },
            live: {
              after: {stableAfter: {actions: 'stable'}},
              on: {'stream.frame': {actions: ['fold', 'remember']}},
            },
          },
          on: {
            'stream.over': [
              {guard: 'canRedial', target: 'waiting', actions: 'lost'},
              {target: 'gaveUp', actions: 'lost'},
            ],
          },
        },
        waiting: {
          id: 'agentWaiting',
          after: {redial: {target: 'open', actions: 'redialed'}},
          on: {retry: {target: 'open', actions: 'retried'}},
        },
        gaveUp: {id: 'agentGaveUp', on: {retry: {target: 'open', actions: 'retried'}}},
      },
    },
    older: {
      initial: 'idle',
      states: {
        idle: {on: {older: 'loading'}},
        loading: {
          invoke: {
            id: 'loadOlder',
            src: 'loadOlder',
            input: ({context, event}) => ({
              ports: context.ports,
              agentId: context.agentId,
              before: event.type === 'older' ? event.before : 1,
            }),
            onDone: {
              target: 'idle',
              actions: [
                sendParent(
                  ({event}): AgentFold => ({
                    type: 'agent.fold',
                    frame: {
                      body: {case: 'older', value: event.output},
                      sequence: event.output.sequence,
                    } as AgentFrame,
                  }),
                ),
                assign({olderError: null}),
              ],
            },
            onError: {
              target: 'idle',
              actions: assign({olderError: ({event}) => faultOf(event.error)}),
            },
          },
          after: {requestDeadline: {target: 'idle', actions: 'olderTimedOut'}},
        },
      },
    },
    message: {
      initial: 'idle',
      states: {
        idle: {
          always: {guard: 'hasQueued', target: 'sending', actions: 'dequeue'},
          on: {'message.send': {guard: 'notPending', target: 'sending', actions: 'startSend'}},
        },
        sending: {
          invoke: {
            id: 'sendMessage',
            src: 'sendMessage',
            input: ({context}) => {
              const sent = context.inflight;
              if (sent === null)
                throw new Error('sendMessage starts only with a message in flight');
              return {
                ports: context.ports,
                command: sendMessageCommand(sent.commandId, sent.agentId, sent.text),
              };
            },
            onDone: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                inflight: null,
                box: {
                  status: 'answered',
                  commandId: context.inflight?.commandId ?? '',
                  receipt: event.output,
                } as MessageBox,
              })),
            },
            onError: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                inflight: null,
                box: {
                  status: 'failed',
                  commandId: context.inflight?.commandId ?? '',
                  fault: faultOf(event.error),
                } as MessageBox,
              })),
            },
          },
          after: {
            requestDeadline: {
              target: 'idle',
              actions: assign(({context}) => ({
                inflight: null,
                box: {
                  status: 'failed',
                  commandId: context.inflight?.commandId ?? '',
                  fault: timeout('The host'),
                } as MessageBox,
              })),
            },
          },
          on: {'message.send': {guard: 'notPending', actions: 'enqueue'}},
        },
      },
      on: {'message.dismiss': {actions: 'dismiss'}},
    },
  },
});
