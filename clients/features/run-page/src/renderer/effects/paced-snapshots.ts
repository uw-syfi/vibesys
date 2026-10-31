import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {createActor} from 'xstate';
import {projectionPace} from '../../state/index.js';

/** Anything that tells a listener each new snapshot, such as an actor. */
export interface Snapshots<S> {
  subscribe(listener: (snapshot: S) => void): {unsubscribe(): void};
}

/**
 * Hand `deliver` the snapshots of `source` the page may project: those that `isBatch` calls a
 * batch of a read that is catching up (given the snapshot before it) wait for the next tick of
 * `projectionPace` on `schedule`, the rest come at once. Returns the stop that cancels a tick
 * still waiting.
 */
export function pacedSnapshots<S>(
  source: Snapshots<S>,
  schedule: ScheduleTimeout,
  isBatch: (snapshot: S, previous: S | undefined) => boolean,
  deliver: (snapshot: S) => void,
): () => void {
  const pace = createActor(projectionPace<S>(), {clock: clockFrom(schedule)});
  let delivered: S | undefined;
  pace.subscribe(({context}) => {
    if (context.shown === undefined || context.shown === delivered) return;
    delivered = context.shown;
    deliver(context.shown);
  });
  pace.start();
  let previous: S | undefined;
  const feed = source.subscribe(value => {
    pace.send({type: 'offer', value, catchingUp: isBatch(value, previous)});
    previous = value;
  });
  return () => {
    feed.unsubscribe();
    pace.stop();
  };
}
