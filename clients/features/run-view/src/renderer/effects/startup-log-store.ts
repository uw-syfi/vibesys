/**
 * The startup log panel's shell: `startupLogMachine` with its operations provided over a
 * `RunLogSource` (the listing, and each follower's read), run on an injected clock, and seen as one
 * store of `StartupLogView` that also follows the invoked follower's own snapshots.
 */
import {logFollowMachineFor, type RunLogSource} from '@vibesys/logs';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {type AnyActorRef, createActor, fromPromise, type Subscription} from 'xstate';
import {
  type FollowerView,
  type StartupLogEvent,
  type StartupLogInput,
  type StartupLogView,
  startupLogMachine,
  startupLogView,
} from '../../state/startup-log.js';

export interface StartupLogStore {
  getState(): StartupLogView;
  subscribe(listener: () => void): () => void;
  send(event: StartupLogEvent): void;
  /** Stop the panel for good: the listing, the follower, and every delay. */
  close(): void;
}

function followerView(ref: AnyActorRef | undefined): FollowerView | null {
  if (ref === undefined) return null;
  const {context} = ref.getSnapshot() as {context: FollowerView};
  return {view: context.view, failure: context.failure};
}

export function createStartupLogStore(
  source: RunLogSource,
  schedule: ScheduleTimeout,
  input: StartupLogInput = {},
): StartupLogStore {
  const machine = startupLogMachine.provide({
    actors: {
      list: fromPromise(({signal}) => source.list(signal)),
      follow: logFollowMachineFor(source),
    },
  });
  const actor = createActor(machine, {input, clock: clockFrom(schedule)});
  const listeners = new Set<() => void>();
  let follower: AnyActorRef | undefined;
  let followerSubscription: Subscription | null = null;
  let parentSnapshot = actor.getSnapshot();
  let followerSnapshot: unknown = null;
  let view = startupLogView(parentSnapshot, null);

  // Publish only a change: an actor notifies on every event, also one that changed nothing.
  const publish = (): void => {
    const nextParent = actor.getSnapshot();
    const nextFollower = follower?.getSnapshot() ?? null;
    if (nextParent === parentSnapshot && nextFollower === followerSnapshot) return;
    parentSnapshot = nextParent;
    followerSnapshot = nextFollower;
    view = startupLogView(nextParent, followerView(follower));
    for (const listener of [...listeners]) listener();
  };
  actor.subscribe(snapshot => {
    const child = snapshot.children['follow'] as AnyActorRef | undefined;
    if (child !== follower) {
      followerSubscription?.unsubscribe();
      follower = child;
      followerSubscription = child?.subscribe(publish) ?? null;
    }
    publish();
  });
  actor.start();

  return {
    getState: () => view,
    subscribe: listener => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    send: event => actor.send(event),
    close: () => {
      followerSubscription?.unsubscribe();
      actor.stop();
      listeners.clear();
    },
  };
}
