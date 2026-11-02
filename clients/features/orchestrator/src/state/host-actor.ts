/** One host's machine with its requests provided, and the view a screen reads from it. */
import {actorStore, type MachineClock} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import {type Actor, createActor, fromPromise} from 'xstate';
import {
  type HostContext,
  type HostEvent,
  type HostInput,
  hostMachine,
  PAGE_SIZE,
} from './host-machine.js';

export type HostActor = Actor<typeof hostMachine>;

/** Where each stream stands. `waiting` is between a lost connection and the next try. */
export interface HostView {
  readonly context: HostContext;
  readonly runs: 'live' | 'waiting';
  readonly agent: 'idle' | 'live' | 'waiting';
  readonly older: 'idle' | 'loading';
  readonly sending: boolean;
  readonly loadingPlans: boolean;
}

export function createHostActor(input: HostInput, clock?: MachineClock): HostActor {
  const machine = hostMachine.provide({
    actors: {
      loadOlder: fromPromise(({input: {ports, agentId, before}}) =>
        ports.listItems(agentId, before, PAGE_SIZE),
      ),
      sendMessage: fromPromise(({input: {ports, command}}) => ports.send(command)),
      loadPlans: fromPromise(({input: {ports, runId}}) => ports.listPlans(runId)),
    },
  });
  return createActor(machine, clock === undefined ? {input} : {input, clock});
}

/** The actor as the store a screen subscribes to; it notifies when the view changes. */
export function hostStore(actor: HostActor): Store<HostView, HostEvent> {
  let last: HostView | null = null;
  return actorStore(actor, snapshot => {
    const value = snapshot.value;
    const next: HostView = {
      context: snapshot.context,
      runs: value.runs,
      agent: value.agent,
      older: value.older,
      sending: value.message === 'sending',
      loadingPlans: value.plans === 'loading',
    };
    const same =
      last !== null &&
      last.context === next.context &&
      last.runs === next.runs &&
      last.agent === next.agent &&
      last.older === next.older;
    if (same && last !== null) return last;
    last = next;
    return next;
  });
}
