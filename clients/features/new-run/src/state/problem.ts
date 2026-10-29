/**
 * Why a load, fix or start of the New run screen failed, in the terms the screen words: which
 * step it was, and what the main process said about it. A failure nobody reported (a bridge call
 * that threw or rejected) carries no text, because its raw error is not for the user.
 */

import type {FailureCause} from '@vibesys/ipc';

/** The operations the screen waits on. */
export type Step =
  | 'features'
  | 'describe'
  | 'harnesses'
  | 'examples'
  | 'tasks'
  | 'goal'
  | 'preflight'
  | 'fix'
  | 'start';

export interface Problem {
  readonly step: Step;
  /** What the main process reported, in its words; null when the failure was not reported. */
  readonly detail: string | null;
  /** Why, when the failure is the machine connection's (typed at the source); else null. */
  readonly cause: FailureCause | null;
}

/** A step's failure as the main process reported it (`detail`), or unreported (null). */
export class StepFailure extends Error {
  readonly detail: string | null;
  readonly reason: FailureCause | null;

  constructor(detail: string | null, reason: FailureCause | null = null) {
    super(detail ?? 'unexpected failure');
    this.detail = detail;
    this.reason = reason;
  }
}

/** The problem `error` is for `step`: only a reported failure's words are kept. */
export function problemOf(step: Step, error: unknown): Problem {
  return error instanceof StepFailure
    ? {step, detail: error.detail, cause: error.reason}
    : {step, detail: null, cause: null};
}
