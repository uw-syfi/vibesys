/**
 * How far a machine has come towards its run list. The connection layer names the step it is on
 * (`ListingStep`), a listing carries it, and `machineLoad` folds a machine's listing into the one
 * value every screen reads: loading at a named step, failed at a named step, or listed. There is
 * no other state, so no screen can wait on something it cannot name.
 */
import type {ConnectionStatus} from './connection-status.js';
import type {HostKey} from './host.js';
import {type HostListing, LISTING_STEPS, type ListingStep} from './listing.js';
import {connectionProblem, problemWord} from './machine-status.js';

const STEP_WORDS: Readonly<Record<ListingStep, string>> = {
  gateway: 'Starting gateway',
  handshake: 'Connecting',
  runs: 'Loading runs',
};

/** Where one machine's run list stands. */
export type MachineLoad =
  /** Working at `step`; `attempt` counts the tries that failed before this one. */
  | {
      readonly kind: 'loading';
      readonly host: HostKey;
      readonly step: ListingStep;
      readonly attempt: number;
    }
  /** Stopped at `step` until the user retries or the machine answers. */
  | {
      readonly kind: 'failed';
      readonly host: HostKey;
      readonly step: ListingStep;
      readonly reason: string;
    }
  | {readonly kind: 'listed'; readonly host: HostKey};

/** The longest reason a machine's block shows. */
export const FAILURE_WORDS_MAX = 48;

const FAILURE_WORDS: readonly (readonly [RegExp, (code: string) => string])[] = [
  [/gateway (?:startup|connect) exited: (\S+?):?(?:\s|$)/, code => `gateway exited (code ${code})`],
  [/requires gateway resources/, () => 'gateway not built'],
  [/timed out|did not connect in time/, () => 'gateway timed out'],
  [/API hash mismatch/, () => 'version mismatch'],
  [/run stream went silent/, () => 'no answer'],
  [/run stream ended/, () => 'stream ended'],
];

/** `detail`, what a failed connection left behind, in a few words. */
export function failureWords(detail: string): string {
  for (const [pattern, words] of FAILURE_WORDS) {
    const found = pattern.exec(detail);
    if (found !== null) return words(found[1] ?? '');
  }
  const line =
    detail
      .replace(/^(?:Error: )+/, '')
      .split('\n')[0]
      ?.trim() ?? '';
  if (line === '') return 'no reason given';
  return line.length <= FAILURE_WORDS_MAX ? line : `${line.slice(0, FAILURE_WORDS_MAX - 1)}…`;
}

function stoppedWords(connection: ConnectionStatus): string {
  if (connection.kind === 'offline') return failureWords(connection.detail);
  const problem = connectionProblem(connection);
  return problem === null ? connection.kind : problemWord(problem);
}

/** `host`'s load as `listing` says; no listing yet is the first step. */
export function machineLoad(host: HostKey, listing: HostListing | undefined): MachineLoad {
  if (listing === undefined) return {kind: 'loading', host, step: LISTING_STEPS[0], attempt: 0};
  if (listing.kind === 'listed') return {kind: 'listed', host};
  const {connection, step} = listing;
  switch (connection.kind) {
    case 'connecting':
    case 'connected':
      return {kind: 'loading', host, step, attempt: 0};
    case 'reconnecting':
      return {kind: 'loading', host, step, attempt: connection.attempt};
    default:
      return {kind: 'failed', host, step, reason: stoppedWords(connection)};
  }
}

export type LoadStepState = 'done' | 'current' | 'pending' | 'failed';

/** One step of a machine's load as a screen lists it. */
export interface LoadStep {
  readonly step: ListingStep;
  readonly word: string;
  readonly state: LoadStepState;
  /** Where the step stands among them (`1/3`). */
  readonly position: string;
}

/** Every step of `load` in order, the step it stands on marked. */
export function loadSteps(load: Exclude<MachineLoad, {readonly kind: 'listed'}>): LoadStep[] {
  const at = LISTING_STEPS.indexOf(load.step);
  return LISTING_STEPS.map((step, index) => ({
    step,
    word: STEP_WORDS[step],
    position: `${index + 1}/${LISTING_STEPS.length}`,
    state:
      index < at ? 'done' : index > at ? 'pending' : load.kind === 'failed' ? 'failed' : 'current',
  }));
}
