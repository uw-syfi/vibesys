/**
 * Why a load, fix or start of the New run screen failed, in the terms the screen words: which
 * step it was, and what the main process said about it. A failure nobody reported (a bridge call
 * that threw or rejected) carries no text, because its raw error is not for the user.
 */

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
}

/** A step's failure as the main process reported it (`detail`), or unreported (null). */
export class StepFailure extends Error {
  readonly detail: string | null;

  constructor(detail: string | null, options?: ErrorOptions) {
    super(detail ?? 'unexpected failure', options);
    this.detail = detail;
  }
}

/** The problem `error` is for `step`: only a reported failure's words are kept. */
export function problemOf(step: Step, error: unknown): Problem {
  return {step, detail: error instanceof StepFailure ? error.detail : null};
}
