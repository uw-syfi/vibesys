/**
 * What a machine's state is called wherever the app shows it: the one status vocabulary and the
 * one word for each problem, so Machines, New run, the Run screen, the rail and the sidebar read
 * the same machine the same way. A status is a function of facts (a connection, a helper's
 * features), never stored.
 */
import type {ConnectionStatus} from './connection-status.js';

/** A machine's status: usable, one of the four things that stop it, or not known yet. */
export type MachineStatus =
  | 'ready'
  /** Its helper lacks a feature this app needs (`REQUIRED_HELPER_FEATURES`). */
  | 'needs-update'
  /** No VibeSys checkout is at its path. */
  | 'checkout-not-found'
  /** Its connection waits for credentials. */
  | 'sign-in'
  /** It cannot be reached (offline, no `uv`, a checkout of another protocol version). */
  | 'unavailable'
  | 'checking';

/** The statuses that stop a machine from being used: each needs the user. */
export type MachineProblem = Exclude<MachineStatus, 'ready' | 'checking'>;

/** The problems from the most to the least urgent, for a place that can show only one. */
export const MACHINE_PROBLEMS: readonly MachineProblem[] = [
  'unavailable',
  'sign-in',
  'needs-update',
  'checkout-not-found',
];

const PROBLEM_WORDS: Readonly<Record<MachineProblem, string>> = {
  'needs-update': 'Needs an update',
  'checkout-not-found': 'Checkout not found',
  'sign-in': 'Sign in',
  unavailable: 'Unreachable',
};

/** The word every screen uses for a machine problem. */
export function problemWord(problem: MachineProblem): string {
  return PROBLEM_WORDS[problem];
}

/** The most urgent of `problems`, or null when there are none. */
export function mostUrgentProblem(problems: readonly MachineProblem[]): MachineProblem | null {
  return MACHINE_PROBLEMS.find(problem => problems.includes(problem)) ?? null;
}

/**
 * Why `connection` will not serve without someone acting, or null while it may yet: a host that
 * needs credentials, a checkout the app cannot speak to, or one that stopped answering.
 */
export function connectionProblem(connection: ConnectionStatus): MachineProblem | null {
  switch (connection.kind) {
    case 'auth-needed':
      return 'sign-in';
    case 'incompatible':
      return 'needs-update';
    case 'offline':
      return 'unavailable';
    default:
      return null;
  }
}
