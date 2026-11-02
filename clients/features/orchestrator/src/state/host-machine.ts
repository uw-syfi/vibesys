/**
 * Control state for one host's orchestrator: the run stream and its redials, the ideas list, and
 * one child actor per open agent (`agent-machine.ts`) that owns that agent's watch, history
 * requests, and message box. Regions coordinate through the state they share, never through each
 * other's states.
 *
 * Every stream is an invoked actor with an id, and every wait has a deadline from an `after`
 * delay: leaving the state that holds a stream, or stopping the actor, runs the actor's cleanup
 * (which closes the watch and so the connection it used), and nothing from a stream that was left
 * can change state because a stopped actor sends nothing. Stopping this machine stops its
 * children. Data folds are `@vibesys/orchestrator-state`'s plain reducers, applied in `assign`.
 */

import type {HostId} from '@vibesys/hosts';
import {unprovided} from '@vibesys/machine';
import type {Plan, WatchRunsResponse} from '@vibesys/orchestrator-proto';
import type {HostPorts} from '@vibesys/orchestrator-source';
import {
  applyAgentFeed,
  applyEvent,
  applySnapshot,
  emptyState,
  type OrchestratorState,
  unbrokenFrom,
} from '@vibesys/orchestrator-state';
import {type ActorRefFrom, assign, fromCallback, sendTo, setup, stopChild} from 'xstate';
import {type AgentEvent, type AgentFold, agentMachine, type Outgoing} from './agent-machine.js';
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

type RunsBody = WatchRunsResponse['body'];

/** How many of an agent's newest items its first page holds, and each older page. */
export const PAGE_SIZE = 40;

export type PlansState =
  | {readonly status: 'idle'}
  | {readonly status: 'loading'; readonly runId: string}
  | {readonly status: 'ready'; readonly runId: string; readonly plans: readonly Plan[]}
  | {readonly status: 'failed'; readonly runId: string; readonly fault: Fault};

export type AgentRef = ActorRefFrom<typeof agentMachine>;

export interface HostContext {
  readonly host: HostId;
  readonly ports: HostPorts;
  readonly backoff: NewBackoff;
  readonly state: OrchestratorState;
  readonly runsAttempts: number;
  readonly runsFault: Fault | null;
  /** The open agents' machines, by agent id. */
  readonly agents: Readonly<Record<string, AgentRef>>;
  readonly plans: PlansState;
}

export type HostEvent =
  | {readonly type: 'runs.frame'; readonly body: RunsBody}
  | {readonly type: 'runs.over'; readonly fault: Fault}
  | {readonly type: 'retry'}
  | {readonly type: 'agent.open'; readonly agentId: string}
  | {readonly type: 'agent.close'; readonly agentId: string}
  | {readonly type: 'agent.retry'; readonly agentId: string}
  | {readonly type: 'agent.older'; readonly agentId: string}
  | {readonly type: 'message.dismiss'; readonly agentId: string}
  | ({readonly type: 'message.send'} & Outgoing)
  | AgentFold
  | {readonly type: 'plans.load'; readonly runId: string};

export interface HostInput {
  readonly host: HostId;
  readonly ports: HostPorts;
  /** The redial schedule: a fresh `BackoffSchedule` per call. */
  readonly backoff: NewBackoff;
}

/** What the run stream actor needs to dial. */
export interface RunsStreamInput {
  readonly ports: HostPorts;
  readonly after: bigint;
}

function foldRuns(state: OrchestratorState, body: RunsBody): OrchestratorState {
  if (body.case === 'snapshot') return applySnapshot(state, body.value);
  return body.case === 'event' ? applyEvent(state, body.value) : state;
}

/** The machine of the agent an event names; the transition's `isOpen` guard has checked it exists. */
function agentRef({context, event}: {context: HostContext; event: HostEvent}): AgentRef {
  const ref = 'agentId' in event ? context.agents[event.agentId] : undefined;
  if (ref === undefined) throw new Error('the agent is not open');
  return ref;
}

export const hostMachine = setup({
  types: {context: {} as HostContext, events: {} as HostEvent, input: {} as HostInput},
  actors: {
    runsStream: fromCallback<HostEvent, RunsStreamInput>(() => {
      throw new Error('runsStream was not provided');
    }),
    agent: agentMachine,
    loadPlans: unprovided<readonly Plan[], {ports: HostPorts; runId: string}>('loadPlans'),
  },
  actions: {
    foldRuns: assign(({context, event}) =>
      event.type === 'runs.frame' ? {state: foldRuns(context.state, event.body)} : {},
    ),
    foldAgent: assign(({context, event}) =>
      event.type === 'agent.fold' ? {state: applyAgentFeed(context.state, event.frame)} : {},
    ),
    runsLost: assign(({event}) => ({runsFault: event.type === 'runs.over' ? event.fault : null})),
    runsTimedOut: assign({runsFault: timeout('The host')}),
    runsRedialed: assign(({context}) => ({runsAttempts: context.runsAttempts + 1})),
    runsRetried: assign({runsAttempts: 0, runsFault: null}),
    runsLive: assign({runsFault: null}),
    runsStable: assign({runsAttempts: 0}),
    // Only one agent is open at a time: opening one stops the others, and a stopped child's
    // watch, history request, and unanswered send end with it.
    openAgent: assign(({context, event, spawn}) => {
      if (event.type !== 'agent.open') return {};
      if (context.agents[event.agentId] !== undefined) return {};
      return {
        agents: {
          [event.agentId]: spawn('agent', {
            id: `agent:${event.agentId}`,
            input: {agentId: event.agentId, ports: context.ports, backoff: context.backoff},
          }),
        },
      };
    }),
    stopOthers: stopChild(({context, event}) => {
      const keep = event.type === 'agent.open' ? event.agentId : '';
      const other = Object.entries(context.agents).find(([id]) => id !== keep);
      return other === undefined ? '' : other[1].id;
    }),
    closeAgent: assign(({context, event}) => {
      if (event.type !== 'agent.close') return {};
      const {[event.agentId]: _gone, ...agents} = context.agents;
      return {agents};
    }),
    stopAgent: stopChild(({context, event}) =>
      event.type === 'agent.close' ? (context.agents[event.agentId]?.id ?? '') : '',
    ),
    retryAgent: sendTo(agentRef, (): AgentEvent => ({type: 'retry'})),
    dismissMessage: sendTo(agentRef, (): AgentEvent => ({type: 'message.dismiss'})),
    sendMessage: sendTo(agentRef, ({event}): AgentEvent => {
      if (event.type !== 'message.send') throw new Error('sendMessage runs on a message');
      return event;
    }),
    pageOlder: sendTo(
      agentRef,
      ({context, event}): AgentEvent => ({
        type: 'older',
        before:
          event.type === 'agent.older' ? (unbrokenFrom(context.state, event.agentId) ?? 1) : 1,
      }),
    ),
    plansLoading: assign(({event}) =>
      event.type === 'plans.load'
        ? {plans: {status: 'loading', runId: event.runId} as PlansState}
        : {},
    ),
  },
  guards: {
    runsFrameGaps: ({context, event}) =>
      event.type === 'runs.frame' && foldRuns(context.state, event.body).gap !== null,
    canRedial: ({context}) => redialDelay(context.backoff, context.runsAttempts) !== undefined,
    // Events for an agent that is not open (closed since, or never opened) are dropped.
    isOpen: ({context, event}) => 'agentId' in event && context.agents[event.agentId] !== undefined,
    canPageOlder: ({context, event}) => {
      if (event.type !== 'agent.older' || context.agents[event.agentId] === undefined) return false;
      const oldest = unbrokenFrom(context.state, event.agentId);
      return oldest !== null && oldest > 1;
    },
  },
  delays: {
    runsRedial: ({context}) => redialDelay(context.backoff, context.runsAttempts) ?? 0,
    connectDeadline: CONNECT_DEADLINE_MS,
    requestDeadline: REQUEST_DEADLINE_MS,
    stableAfter: STABLE_AFTER_MS,
  },
}).createMachine({
  id: 'orchestrator-host',
  type: 'parallel',
  context: ({input}) => ({
    host: input.host,
    ports: input.ports,
    backoff: input.backoff,
    state: emptyState,
    runsAttempts: 0,
    runsFault: null,
    agents: {},
    plans: {status: 'idle'},
  }),
  states: {
    runs: {
      id: 'runs',
      initial: 'open',
      states: {
        // The stream is dialled on entering and closed on leaving, however the state is left.
        open: {
          id: 'runsOpen',
          invoke: {
            id: 'runsStream',
            src: 'runsStream',
            input: ({context}) => ({
              ports: context.ports,
              after: context.state.gap === null ? context.state.sequence : 0n,
            }),
          },
          initial: 'connecting',
          states: {
            connecting: {
              after: {
                connectDeadline: [
                  {guard: 'canRedial', target: '#runsWaiting', actions: 'runsTimedOut'},
                  {target: '#runsGaveUp', actions: 'runsTimedOut'},
                ],
              },
              on: {
                'runs.frame': [
                  {guard: 'runsFrameGaps', target: '#runsOpen', reenter: true, actions: 'foldRuns'},
                  {target: 'live', actions: ['foldRuns', 'runsLive']},
                ],
              },
            },
            live: {
              after: {stableAfter: {actions: 'runsStable'}},
              on: {
                'runs.frame': [
                  {guard: 'runsFrameGaps', target: '#runsOpen', reenter: true, actions: 'foldRuns'},
                  {actions: 'foldRuns'},
                ],
              },
            },
          },
          on: {
            'runs.over': [
              {guard: 'canRedial', target: 'waiting', actions: 'runsLost'},
              {target: 'gaveUp', actions: 'runsLost'},
            ],
          },
        },
        waiting: {
          id: 'runsWaiting',
          after: {runsRedial: {target: 'open', actions: 'runsRedialed'}},
          on: {retry: {target: 'open', actions: 'runsRetried'}},
        },
        gaveUp: {id: 'runsGaveUp', on: {retry: {target: 'open', actions: 'runsRetried'}}},
      },
    },
    agents: {
      on: {
        'agent.open': {actions: ['stopOthers', 'openAgent']},
        'agent.close': {guard: 'isOpen', actions: ['stopAgent', 'closeAgent']},
        'agent.fold': {actions: 'foldAgent'},
        'agent.retry': {guard: 'isOpen', actions: 'retryAgent'},
        'agent.older': {guard: 'canPageOlder', actions: 'pageOlder'},
        'message.send': {guard: 'isOpen', actions: 'sendMessage'},
        'message.dismiss': {guard: 'isOpen', actions: 'dismissMessage'},
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
            onDone: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                plans: {
                  status: 'ready',
                  runId: context.plans.status === 'loading' ? context.plans.runId : '',
                  plans: event.output,
                } as PlansState,
              })),
            },
            onError: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                plans: {
                  status: 'failed',
                  runId: context.plans.status === 'loading' ? context.plans.runId : '',
                  fault: faultOf(event.error),
                } as PlansState,
              })),
            },
          },
          after: {
            requestDeadline: {
              target: 'idle',
              actions: assign(({context}) => ({
                plans: {
                  status: 'failed',
                  runId: context.plans.status === 'loading' ? context.plans.runId : '',
                  fault: timeout('The host'),
                } as PlansState,
              })),
            },
          },
        },
      },
      on: {'plans.load': {target: '.loading', reenter: true, actions: 'plansLoading'}},
    },
  },
});
