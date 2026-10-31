/**
 * When the page may project what the loader has read. The loader publishes after every batch of a
 * run's history, and each publication that the page projects costs a pass over the whole skeleton
 * (twice: the run model and the screen state), so a long history is read faster than it can be
 * drawn. While a read is catching up, the newest publication is held back and shown at most once
 * per `PROJECTION_PACE_MS`; the first one after a quiet spell is shown at once, and so is anything
 * that is not catching up (the read's completion, a failure), whatever was held.
 */
import {assign, setup} from 'xstate';

export const PROJECTION_PACE_MS = 500;

interface ProjectionOffer<T> {
  readonly type: 'offer';
  readonly value: T;
  /** The read is still catching up, so the value may wait for the next tick. */
  readonly catchingUp: boolean;
}

interface PaceContext<T> {
  /** The newest value to project; changes only when the page is let to project. */
  readonly shown: T | undefined;
  /** A catch-up value that arrived since `shown`, waiting for the tick. */
  readonly held: T | undefined;
}

export function projectionPace<T>() {
  return setup({
    types: {context: {} as PaceContext<T>, events: {} as ProjectionOffer<T>},
    delays: {tick: PROJECTION_PACE_MS},
    guards: {
      catchingUp: ({event}) => event.catchingUp,
      holding: ({context}) => context.held !== undefined,
    },
    actions: {
      show: assign(({event}) => ({shown: event.value, held: undefined})),
      hold: assign(({event}) => ({held: event.value})),
      release: assign(({context}) => ({shown: context.held, held: undefined})),
    },
  }).createMachine({
    id: 'projectionPace',
    context: {shown: undefined, held: undefined},
    initial: 'open',
    states: {
      open: {
        on: {
          offer: [{guard: 'catchingUp', target: 'cooling', actions: 'show'}, {actions: 'show'}],
        },
      },
      cooling: {
        on: {
          offer: [
            {guard: 'catchingUp', actions: 'hold'},
            {target: 'open', actions: 'show'},
          ],
        },
        after: {
          tick: [
            {guard: 'holding', target: 'cooling', reenter: true, actions: 'release'},
            {target: 'open'},
          ],
        },
      },
    },
  });
}
