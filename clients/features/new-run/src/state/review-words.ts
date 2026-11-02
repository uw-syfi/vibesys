/**
 * The words of Review that are not the user's or the machine's: the agent's sign-in check and what
 * the roles wait for before the inputs can be checked. Pure; the page only lays them out.
 */
import type {InputsWait} from './form.js';

/** The sign-in check of the agent's CLI `harness` on `machine`: its label and each detail. */
export function signInWords(
  harness: string,
  machine: string,
): {readonly label: string; readonly signedIn: string; readonly signedOut: string} {
  return {
    label: `${harness} signed in on ${machine}`,
    signedIn: `Signed in on ${machine}.`,
    signedOut: `Sign in once from a terminal on ${machine}.`,
  };
}

/** The sign-in check's fix. */
export const OPEN_LOGINS_WORDS = 'Open Agent logins';

const WAITS: Readonly<Record<InputsWait, {readonly short: string; readonly sentence: string}>> = {
  project: {
    short: 'After the project is made',
    sentence: 'The roles are read from the machine once the project is made.',
  },
  task: {
    short: 'After a task is chosen',
    sentence: 'The roles are read from the machine once a task is chosen.',
  },
  agent: {
    short: 'After an agent is chosen',
    sentence: 'The roles are read from the machine once an agent is chosen.',
  },
  arguments: {
    short: 'After the extra arguments are fixed',
    sentence: 'The roles are read from the machine once the extra arguments are fixed.',
  },
};

/** What the roles wait for, as a row's status (`short`) or a line of its own (`sentence`). */
export function rolesWaitWords(on: InputsWait): {
  readonly short: string;
  readonly sentence: string;
} {
  return WAITS[on];
}
