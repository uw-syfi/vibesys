/** One host's machine with its requests provided, and the view a screen reads from it. */
import type {MachineClock} from '@vibesys/machine';
import {createListenerDelivery, type Store} from '@vibesys/store';
import {type Actor, type AnyActorRef, createActor, fromCallback, fromPromise} from 'xstate';
import {
  type AgentEvent,
  type AgentStreamInput,
  agentMachine,
  type MessageBox,
} from './agent-machine.js';
import {type Fault, faultOf} from './faults.js';
import {
  type AgentRef,
  type HostContext,
  type HostEvent,
  type HostInput,
  hostMachine,
  PAGE_SIZE,
  type RunsStreamInput,
} from './host-machine.js';

export type HostActor = Actor<typeof hostMachine>;

/**
 * Where a stream stands: `connecting` before its first answer, `waiting` between tries, and
 * `gave-up` once every try of the schedule failed (only a Retry dials again).
 */
export type StreamStatus = 'connecting' | 'live' | 'waiting' | 'gave-up';

/** The agent being watched, as a screen reads it. */
export interface AgentView {
  readonly agentId: string;
  readonly stream: StreamStatus;
  readonly fault: Fault | null;
  readonly older: 'idle' | 'loading';
  readonly olderError: Fault | null;
  readonly box: MessageBox;
}

/** Where each stream stands, and the open agent's own view. */
export interface HostView {
  readonly context: HostContext;
  readonly runs: StreamStatus;
  /** The one open agent, or null. */
  readonly agent: AgentView | null;
  readonly loadingPlans: boolean;
}

/**
 * Read a watch to its end, handing each frame to the machine. The returned function closes the
 * watch, which is how the actor's cleanup releases the stream whenever the actor is stopped.
 */
function pump<T, E>(
  open: () => {readonly frames: AsyncIterable<T>; close(): void},
  frame: (body: T) => E,
  over: (fault: Fault) => E,
  send: (event: E) => void,
): () => void {
  const watch = open();
  let closed = false;
  void (async () => {
    try {
      for await (const body of watch.frames) send(frame(body));
      if (!closed) send(over({kind: 'ended', detail: 'the host closed the stream'}));
    } catch (error) {
      if (!closed) send(over(faultOf(error)));
    }
  })();
  return () => {
    closed = true;
    watch.close();
  };
}

export function createHostActor(input: HostInput, clock?: MachineClock): HostActor {
  const agent = agentMachine.provide({
    actors: {
      stream: fromCallback<AgentEvent, AgentStreamInput>(
        ({input: {ports, agentId, after}, sendBack}) =>
          pump(
            () => ports.openAgent(agentId, PAGE_SIZE, after),
            (frame): AgentEvent => ({type: 'stream.frame', frame}),
            (fault): AgentEvent => ({type: 'stream.over', fault}),
            sendBack,
          ),
      ),
      loadOlder: fromPromise(({input: {ports, agentId, before}, signal}) =>
        ports.listItems(agentId, before, PAGE_SIZE, signal),
      ),
      sendMessage: fromPromise(({input: {ports, command}, signal}) => ports.send(command, signal)),
    },
  });
  const machine = hostMachine.provide({
    actors: {
      agent,
      runsStream: fromCallback<HostEvent, RunsStreamInput>(({input: {ports, after}, sendBack}) =>
        pump(
          () => ports.openRuns(after),
          (body): HostEvent => ({type: 'runs.frame', body}),
          (fault): HostEvent => ({type: 'runs.over', fault}),
          sendBack,
        ),
      ),
      loadPlans: fromPromise(({input: {ports, runId}, signal}) => ports.listPlans(runId, signal)),
    },
  });
  return createActor(machine, clock === undefined ? {input} : {input, clock});
}

function streamOf(value: unknown): StreamStatus {
  if (value === 'waiting') return 'waiting';
  if (value === 'gaveUp') return 'gave-up';
  if (typeof value === 'object' && value !== null) {
    return Object.values(value)[0] === 'live' ? 'live' : 'connecting';
  }
  return 'connecting';
}

function agentViewOf(ref: AgentRef, last: AgentView | null): AgentView {
  const {context, value} = ref.getSnapshot();
  const next: AgentView = {
    agentId: context.agentId,
    stream: streamOf(value.stream),
    fault: context.fault,
    older: value.older,
    olderError: context.olderError,
    box: context.box,
  };
  const same =
    last !== null &&
    last.agentId === next.agentId &&
    last.stream === next.stream &&
    last.fault === next.fault &&
    last.older === next.older &&
    last.olderError === next.olderError &&
    last.box === next.box;
  return same && last !== null ? last : next;
}

/**
 * The actor as the store a screen subscribes to. It notifies when the host's view changes, and
 * also when the open agent's own machine does, which the host's snapshot does not report.
 */
export function hostStore(actor: HostActor): Store<HostView, HostEvent> {
  const delivery = createListenerDelivery();
  const watched = new Set<AnyActorRef>();
  const compute = (last: HostView | null): HostView => {
    const snapshot = actor.getSnapshot();
    const ref = Object.values(snapshot.context.agents)[0];
    const agent = ref === undefined ? null : agentViewOf(ref, last?.agent ?? null);
    const runs = streamOf(snapshot.value.runs);
    const loadingPlans = snapshot.value.plans === 'loading';
    const same =
      last !== null &&
      last.context === snapshot.context &&
      last.runs === runs &&
      last.agent === agent &&
      last.loadingPlans === loadingPlans;
    return same && last !== null ? last : {context: snapshot.context, runs, agent, loadingPlans};
  };
  let current = compute(null);
  const refresh = (): void => {
    const next = compute(current);
    if (next === current) return;
    current = next;
    delivery.notify();
  };
  const watchChildren = (): void => {
    for (const ref of Object.values(actor.getSnapshot().context.agents)) {
      if (watched.has(ref)) continue;
      watched.add(ref);
      ref.subscribe(refresh);
    }
  };
  actor.subscribe(() => {
    watchChildren();
    refresh();
  });
  return {
    getState: () => current,
    subscribe: delivery.subscribe,
    dispatch: event => actor.send(event),
  };
}
