/** Faults laid over a `Recording`: the link drops, a frame repeats, a stream stalls. */
import {RpcStatus} from '../../core/errors.js';
import type {Recording, Step} from './fake-transport.js';

export interface FaultPlan {
  /**
   * `dropAfter[n]` is how many frames the `n`th call to a method delivers before the link is lost
   * (an end with status 14). A call past the end of the list is not dropped.
   */
  readonly dropAfter: ReadonlyArray<number>;
  /** Indices, in a call's own frames, of the frames delivered twice in a row. */
  readonly duplicate: ReadonlyArray<number>;
  /** After this many frames a call stalls, never ending, until it is cancelled. */
  readonly stallAt?: number;
}

/** The step that replaces the next frame when the call's faults say it never arrives, if any. */
const cutBefore = (plan: FaultPlan, nth: number, delivered: number): Step | undefined => {
  if (plan.stallAt !== undefined && delivered >= plan.stallAt) return {hang: true};
  const dropAfter = plan.dropAfter[nth];
  if (dropAfter !== undefined && delivered >= dropAfter)
    return {end: {message: 'connection lost', status: RpcStatus.UNAVAILABLE}};
  return undefined;
};

/** `steps` with the faults of `plan` for the `nth` call; frames are never altered, only repeated or cut. */
export const faultySteps = (
  steps: ReadonlyArray<Step>,
  plan: FaultPlan,
  nth: number,
): ReadonlyArray<Step> => {
  const duplicated = new Set(plan.duplicate);
  const out: Step[] = [];
  let delivered = 0;
  for (const step of steps) {
    if (!('frame' in step)) {
      out.push(step);
      continue;
    }
    const cut = cutBefore(plan, nth, delivered);
    if (cut !== undefined) return [...out, cut];
    out.push(step);
    if (duplicated.has(delivered)) out.push(step);
    delivered += 1;
  }
  return out;
};

/** A recording that plays `base` with the faults of `plan`. */
export const withFaults = (base: Recording, plan: FaultPlan): Recording => ({
  respond: (target, nth) => faultySteps(base.respond(target, nth), plan, nth),
});
