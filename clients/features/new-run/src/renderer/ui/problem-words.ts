/**
 * What the New run screen says when a load, fix or start failed: a title that names what failed,
 * the main process's own words when it reported one, else a plain line that something unexpected
 * happened. A raw error (`bridge.x is not a function`) is never shown. A load or check can be tried
 * again from the status; a fix or start is pressed again where it was pressed, except a start that
 * may have happened, which waits until the person has looked among their runs.
 */
import {FAILURE_CAUSE_WORDS, UNEXPECTED_FAILURE} from '@vibesys/ipc';
import {type Problem, type Step, startUnconfirmed} from '../../state/index.js';

export interface ProblemWords {
  /** The few words the status shows: what did not happen. */
  readonly label: string;
  /** The sentence that names what failed, the first line of the status's description. */
  readonly title: string;
  readonly body: string;
  /**
   * The raw text behind a typed cause (paths, exit codes), for a tooltip; null when the body is
   * already the whole account.
   */
  readonly detail: string | null;
  /** Whether the status offers "Try again" (`retry`). */
  readonly retry: boolean;
  /** Whether the run may have started, so the status sends the person to their runs first. */
  readonly look: boolean;
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

/** The few words of each step's status; the sentence of `TITLES` is its description. */
const LABELS: Readonly<Record<Step, string>> = {
  features: 'Not checked',
  describe: 'Not read',
  harnesses: 'Not read',
  examples: 'Not read',
  tasks: 'Not read',
  goal: 'Not read',
  preflight: 'Not checked',
  fix: 'Not prepared',
  start: 'Not started',
};

/** What the status says of a start that may have happened. */
export const MAY_HAVE_STARTED = 'May have started';

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
  'Something unexpected happened. The run may have started: look in Runs before starting again.';

export function problemWords(problem: Problem, machine: string): ProblemWords {
  const typed = problem.cause !== null && problem.detail !== null;
  const look = startUnconfirmed(problem);
  return {
    label: look ? MAY_HAVE_STARTED : LABELS[problem.step],
    title: TITLES[problem.step](machine),
    // A connection failure is a status word, its raw text a tooltip; a failure the main process
    // worded itself is shown as it said it.
    body: typed
      ? FAILURE_CAUSE_WORDS[problem.cause as NonNullable<Problem['cause']>]
      : (problem.detail ?? (look ? START_UNKNOWN : UNEXPECTED_FAILURE)),
    detail: typed ? problem.detail : null,
    retry: RETRYABLE[problem.step],
    look,
  };
}
