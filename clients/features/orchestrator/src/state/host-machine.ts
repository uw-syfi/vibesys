/**
 * Control state for one host's orchestrator: the run stream and its redials, the one agent being
 * watched (and paging older history), the message box, and the ideas list. Four regions that
 * coordinate through the state they share, never through each other's states.
 *
 * Every stream is a `Watch` the machine opened and owns: leaving the state that holds it closes it,
 * and a frame, end or failure from any other watch is dropped, so nothing from a replaced stream
 * can change state. Dialling is synchronous (a stream fails on its first read, as a connection
 * does), so the only invoked operations are single requests: older items, a message, the ideas.
 * Data folds are `@vibesys/orchestrator-state`'s plain reducers, applied in `assign`.
 */

import {unprovided} from '@vibesys/machine';
import {
  type Command,
  type ItemsPage,
  type Plan,
  type Receipt,
  sendMessageCommand,
  type WatchAgentResponse,
  type WatchRunsResponse,
} from '@vibesys/orchestrator-proto';
import type {HostPorts, Watch} from '@vibesys/orchestrator-source';
import {
  applyAgentFeed,
  applyEvent,
  applySnapshot,
  emptyState,
  type OrchestratorState,
  oldestOrdinal,
} from '@vibesys/orchestrator-state';
import {assign, setup} from 'xstate';

type RunsBody = WatchRunsResponse['body'];
type AgentBody = WatchAgentResponse['body'];

/** How many of an agent's newest items its first page holds, and each older page. */
export const PAGE_SIZE = 40;
const REDIAL_BASE_MS = 500;
const REDIAL_MAX_MS = 30_000;

export type MessageBox =
  | {readonly status: 'idle'}
  | {readonly status: 'sending'; readonly agentId: string}
  | {readonly status: 'answered'; readonly receipt: Receipt}
  | {readonly status: 'failed'; readonly error: string};

export type PlansState =
  | {readonly status: 'idle'}
  | {readonly status: 'loading'; readonly runId: string}
  | {readonly status: 'ready'; readonly runId: string; readonly plans: readonly Plan[]}
  | {readonly status: 'failed'; readonly runId: string; readonly error: string};

export interface HostContext {
  readonly host: string;
  readonly ports: HostPorts;
  readonly state: OrchestratorState;
  readonly runsWatch: Watch<RunsBody> | null;
  readonly runsAttempts: number;
  readonly runsError: string | null;
  readonly agentId: string | null;
  readonly agentWatch: Watch<AgentBody> | null;
  readonly agentAttempts: number;
  readonly olderError: string | null;
  readonly message: MessageBox;
  readonly plans: PlansState;
}

export type HostEvent =
  | {readonly type: 'runs.frame'; readonly from: Watch<RunsBody>; readonly body: RunsBody}
  | {readonly type: 'runs.over'; readonly from: Watch<RunsBody>; readonly reason: string | null}
  | {readonly type: 'retry'}
  | {readonly type: 'agent.open'; readonly agentId: string}
  | {readonly type: 'agent.close'}
  | {readonly type: 'agent.frame'; readonly from: Watch<AgentBody>; readonly body: AgentBody}
  | {readonly type: 'agent.over'; readonly from: Watch<AgentBody>; readonly reason: string | null}
  | {readonly type: 'agent.older'}
  | {
      readonly type: 'message.send';
      readonly commandId: string;
      readonly agentId: string;
      readonly text: string;
    }
  | {readonly type: 'message.dismiss'}
  | {readonly type: 'plans.load'; readonly runId: string};

export interface HostInput {
  readonly host: string;
  readonly ports: HostPorts;
}

function foldRuns(state: OrchestratorState, body: RunsBody): OrchestratorState {
  if (body.case === 'snapshot') return applySnapshot(state, body.value);
  return body.case === 'event' ? applyEvent(state, body.value) : state;
}

export const redialDelay = (attempts: number): number =>
  Math.min(REDIAL_MAX_MS, REDIAL_BASE_MS * 2 ** attempts);

export const hostMachine = setup({
  types: {context: {} as HostContext, events: {} as HostEvent, input: {} as HostInput},
  actors: {
    loadOlder: unprovided<ItemsPage, {ports: HostPorts; agentId: string; before: number}>(
      'loadOlder',
    ),
    sendMessage: unprovided<Receipt, {ports: HostPorts; command: Command}>('sendMessage'),
    loadPlans: unprovided<readonly Plan[], {ports: HostPorts; runId: string}>('loadPlans'),
  },
  actions: {
    openRuns: assign(({context}) => ({
      runsWatch: context.ports.openRuns(context.state.gap === null ? context.state.sequence : 0n),
    })),
    closeRuns: ({context}) => context.runsWatch?.close(),
    pumpRuns: ({context, self}) => {
      const watch = context.runsWatch;
      if (watch !== null) void pump(watch.frames, self, 'runs', watch);
    },
    openAgent: assign(({context}) =>
      context.agentId === null
        ? {}
        : {agentWatch: context.ports.openAgent(context.agentId, PAGE_SIZE, 0n)},
    ),
    closeAgent: ({context}) => context.agentWatch?.close(),
    pumpAgent: ({context, self}) => {
      const watch = context.agentWatch;
      if (watch !== null) void pump(watch.frames, self, 'agent', watch);
    },
    foldRuns: assign(({context, event}) =>
      event.type === 'runs.frame'
        ? {state: foldRuns(context.state, event.body), runsAttempts: 0, runsError: null}
        : {},
    ),
    runsLost: assign(({context, event}) => ({
      runsAttempts: context.runsAttempts + 1,
      runsError: event.type === 'runs.over' ? event.reason : null,
    })),
    foldAgent: assign(({context, event}) =>
      event.type === 'agent.frame'
        ? {state: applyAgentFeed(context.state, event.body), agentAttempts: 0}
        : {},
    ),
    agentLost: assign(({context}) => ({agentAttempts: context.agentAttempts + 1})),
    selectAgent: assign(({event}) =>
      event.type === 'agent.open'
        ? {agentId: event.agentId, agentAttempts: 0, olderError: null}
        : {},
    ),
    clearAgent: assign({agentId: null, agentWatch: null, olderError: null}),
    foldOlder: assign(({context, event}) => {
      const page = (event as unknown as {output: ItemsPage}).output;
      return {state: applyAgentFeed(context.state, {case: 'older', value: page}), olderError: null};
    }),
    olderFailed: assign({olderError: 'Older steps could not be loaded.'}),
    sending: assign(({event}) =>
      event.type === 'message.send'
        ? {message: {status: 'sending', agentId: event.agentId} as MessageBox}
        : {},
    ),
    answered: assign(({event}) => ({
      message: {
        status: 'answered',
        receipt: (event as unknown as {output: Receipt}).output,
      } as MessageBox,
    })),
    sendFailed: assign({
      message: {status: 'failed', error: 'The message could not be sent.'} as MessageBox,
    }),
    dismiss: assign({message: {status: 'idle'} as MessageBox}),
    plansLoading: assign(({event}) =>
      event.type === 'plans.load'
        ? {plans: {status: 'loading', runId: event.runId} as PlansState}
        : {},
    ),
    plansReady: assign(({context, event}) => ({
      plans: {
        status: 'ready',
        runId: context.plans.status === 'loading' ? context.plans.runId : '',
        plans: (event as unknown as {output: readonly Plan[]}).output,
      } as PlansState,
    })),
    plansFailed: assign(({context}) => ({
      plans: {
        status: 'failed',
        runId: context.plans.status === 'loading' ? context.plans.runId : '',
        error: 'The ideas could not be read.',
      } as PlansState,
    })),
  },
  guards: {
    currentRuns: ({context, event}) =>
      (event.type === 'runs.frame' || event.type === 'runs.over') &&
      event.from === context.runsWatch,
    runsFrameGaps: ({context, event}) =>
      event.type === 'runs.frame' &&
      event.from === context.runsWatch &&
      foldRuns(context.state, event.body).gap !== null,
    currentAgent: ({context, event}) =>
      (event.type === 'agent.frame' || event.type === 'agent.over') &&
      event.from === context.agentWatch,
    canPageOlder: ({context}) => {
      if (context.agentId === null) return false;
      const oldest = oldestOrdinal(context.state, context.agentId);
      return oldest !== null && oldest > 1;
    },
  },
  delays: {
    runsRedial: ({context}) => redialDelay(context.runsAttempts),
    agentRedial: ({context}) => redialDelay(context.agentAttempts),
  },
}).createMachine({
  id: 'orchestrator-host',
  type: 'parallel',
  context: ({input}) => ({
    host: input.host,
    ports: input.ports,
    state: emptyState,
    runsWatch: null,
    runsAttempts: 0,
    runsError: null,
    agentId: null,
    agentWatch: null,
    agentAttempts: 0,
    olderError: null,
    message: {status: 'idle'},
    plans: {status: 'idle'},
  }),
  states: {
    runs: {
      initial: 'live',
      states: {
        live: {
          entry: ['openRuns', 'pumpRuns'],
          exit: 'closeRuns',
          on: {
            'runs.frame': [
              {guard: 'runsFrameGaps', target: 'live', reenter: true, actions: 'foldRuns'},
              {guard: 'currentRuns', actions: 'foldRuns'},
            ],
            'runs.over': {guard: 'currentRuns', target: 'waiting', actions: 'runsLost'},
          },
        },
        waiting: {
          after: {runsRedial: 'live'},
          on: {retry: 'live'},
        },
      },
    },
    agent: {
      initial: 'idle',
      states: {
        idle: {},
        live: {
          entry: ['openAgent', 'pumpAgent'],
          exit: 'closeAgent',
          on: {
            'agent.frame': {guard: 'currentAgent', actions: 'foldAgent'},
            'agent.over': {guard: 'currentAgent', target: 'waiting', actions: 'agentLost'},
          },
        },
        waiting: {after: {agentRedial: 'live'}},
      },
      on: {
        'agent.open': {target: '.live', reenter: true, actions: 'selectAgent'},
        'agent.close': {target: '.idle', actions: 'clearAgent'},
      },
    },
    older: {
      initial: 'idle',
      states: {
        idle: {on: {'agent.older': {guard: 'canPageOlder', target: 'loading'}}},
        loading: {
          invoke: {
            id: 'loadOlder',
            src: 'loadOlder',
            input: ({context}) => ({
              ports: context.ports,
              agentId: context.agentId ?? '',
              before: oldestOrdinal(context.state, context.agentId ?? '') ?? 1,
            }),
            onDone: {target: 'idle', actions: 'foldOlder'},
            onError: {target: 'idle', actions: 'olderFailed'},
          },
          // Another agent's history is not this one's: leaving it stops the request.
          on: {'agent.open': 'idle', 'agent.close': 'idle'},
        },
      },
    },
    message: {
      initial: 'idle',
      states: {
        idle: {},
        sending: {
          invoke: {
            id: 'sendMessage',
            src: 'sendMessage',
            input: ({context, event}) => ({
              ports: context.ports,
              command: sendCommand(event),
            }),
            onDone: {target: 'idle', actions: 'answered'},
            onError: {target: 'idle', actions: 'sendFailed'},
          },
        },
      },
      on: {
        'message.send': {target: '.sending', reenter: true, actions: 'sending'},
        'message.dismiss': {target: '.idle', actions: 'dismiss'},
      },
    },
    plans: {
      initial: 'idle',
      states: {
        idle: {},
        loading: {
          invoke: {
            id: 'loadPlans',
            src: 'loadPlans',
            input: ({context}) => ({
              ports: context.ports,
              runId: context.plans.status === 'loading' ? context.plans.runId : '',
            }),
            onDone: {target: 'idle', actions: 'plansReady'},
            onError: {target: 'idle', actions: 'plansFailed'},
          },
        },
      },
      on: {'plans.load': {target: '.loading', reenter: true, actions: 'plansLoading'}},
    },
  },
});

function sendCommand(event: HostEvent): Command {
  if (event.type !== 'message.send') throw new Error('sendMessage starts only on message.send');
  return sendMessageCommand(event.commandId, event.agentId, event.text);
}

interface Sender {
  send(event: HostEvent): void;
}

/** Read a watch to its end, handing each frame to the machine; the machine decides what a frame means. */
async function pump<T extends RunsBody | AgentBody>(
  frames: AsyncIterable<T>,
  machine: Sender,
  kind: 'runs' | 'agent',
  from: Watch<T>,
): Promise<void> {
  const send = (event: unknown): void => machine.send(event as HostEvent);
  try {
    for await (const body of frames) send({type: `${kind}.frame`, from, body});
    send({type: `${kind}.over`, from, reason: null});
  } catch (error) {
    send({
      type: `${kind}.over`,
      from,
      reason: error instanceof Error ? error.message : String(error),
    });
  }
}
