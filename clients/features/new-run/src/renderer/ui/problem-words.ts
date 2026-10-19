/**
 * What the New run screen says when a load, fix or start failed: a title that names what failed,
 * the main process's own words when it reported one, else a plain line that something unexpected
 * happened. A raw error (`bridge.x is not a function`) is never shown. A load or check can be tried
 * again from the banner; a fix or start is pressed again where it was pressed.
 */
import {FAILURE_CAUSE_WORDS, UNEXPECTED_FAILURE} from '@vibesys/ipc';
import type {Problem, Step} from '../../state/index.js';

export interface ProblemWords {
  readonly title: string;
  readonly body: string;
  /**
   * The raw text behind a typed cause (paths, exit codes), for a tooltip; null when the body is
   * already the whole account.
   */
  readonly detail: string | null;
  /** Whether the banner offers "Try again" (`retry`). */
  readonly retry: boolean;
}

/** The title for each step, naming `machine` where the step is about it. */
const TITLES: Readonly<Record<Step, (machine: string) => string>> = {
  features: machine => `Could not ask ${machine} what it supports.`,
  describe: machine => `Could not read what ${machine} offers.`,
  harnesses: machine => `Could not read the agents ${machine} runs.`,
  examples: machine => `Could not read the examples on ${machine}.`,
  tasks: () => "Could not read the project's tasks.",
  goal: () => "Could not read the task's goal.",
  preflight: () => 'Could not check whether the run can start.',
  fix: () => 'Could not prepare the project.',
  start: () => 'Could not start the run.',
};

const RETRYABLE: Readonly<Record<Step, boolean>> = {
  features: true,
  describe: true,
  harnesses: true,
  examples: true,
  tasks: true,
  goal: true,
  preflight: true,
  fix: false,
  start: false,
};

/** A start that threw may have started: say so rather than invite a second run. */
const START_UNKNOWN =
  'Something unexpected happened. The run may have started: look in Recent before starting again.';

export function problemWords(problem: Problem, machine: string): ProblemWords {
  const unreported = problem.detail === null;
  const typed = problem.cause !== null && problem.detail !== null;
  return {
    title: TITLES[problem.step](machine),
    // A connection failure is a status word, its raw text a tooltip; a failure the main process
    // worded itself is shown as it said it.
    body: typed
      ? FAILURE_CAUSE_WORDS[problem.cause as NonNullable<Problem['cause']>]
      : (problem.detail ??
        (problem.step === 'start' && unreported ? START_UNKNOWN : UNEXPECTED_FAILURE)),
    detail: typed ? problem.detail : null,
    retry: RETRYABLE[problem.step],
  };
}
