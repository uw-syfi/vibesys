/**
 * What the screens say when the host cannot read a run's job log, by the reason it sends
 * (`Unreadable.reason`): a label for a chip, and the account behind it for a hover. A screen
 * follows the account with since when and with the host's own words (`Unreadable.detail`) as
 * Details. Every reason has its own words here, so a reason the protocol adds fails the type check
 * instead of being shown in another reason's words.
 *
 * A reason that is a fact the app already names elsewhere says it in those words: a missing job
 * tool is the refusal cause's label, a first read in flight is "not read yet" as on the Runs list,
 * and, as a host's status says of a host, a broken link leaves the search "stale" and a job service
 * that never answered is "unreachable".
 */
import {UnreadableReason} from '@vibesys/orchestrator-proto';
import {causeWord} from '../refusal-words.js';
import type {Word} from './words.js';

/** A reason's label and tone, and the account behind them. */
export interface UnreadableWord extends Word {
  readonly hint: string;
}

/** Said of every reason under which the host keeps what it read before. */
const KEPT = 'What was measured before stays shown.';

const UNREADABLE: Readonly<Record<UnreadableReason, UnreadableWord>> = {
  [UnreadableReason.UNSPECIFIED]: {
    label: 'Search unreadable',
    tone: 'warn',
    hint: `The host cannot read the run’s job log and did not say why. ${KEPT}`,
  },
  // The host sends this until its first read of the job log has finished: nothing has been read
  // yet, so nothing shown is out of date.
  [UnreadableReason.NO_DAEMON]: {
    label: 'Search not read yet',
    tone: 'neutral',
    hint: 'The host is still reading the run’s job log for the first time.',
  },
  [UnreadableReason.START_FAILED]: {
    label: 'Job service failed',
    tone: 'warn',
    hint: `The host could not start the job service, so it cannot read the run’s job log. ${KEPT}`,
  },
  [UnreadableReason.CLI_NOT_INSTALLED]: {
    label: causeWord('job-tool-missing'),
    tone: 'warn',
    hint: `The host has no job tool (vibesys-cli) it can use, so it cannot read the run’s job log. ${KEPT}`,
  },
  [UnreadableReason.CLIENT_TOO_OLD]: {
    label: 'Job service too new',
    tone: 'warn',
    hint: `The job service is newer than the host program can read. ${KEPT}`,
  },
  [UnreadableReason.DAEMON_TOO_OLD]: {
    label: 'Job service too old',
    tone: 'warn',
    hint: `The job service is older than the host program needs. ${KEPT}`,
  },
  [UnreadableReason.STORE_OWNED_ELSEWHERE]: {
    label: 'Job store in use',
    tone: 'warn',
    hint: `Another job service or machine owns the task’s job store, so this host cannot read the run’s job log. ${KEPT}`,
  },
  [UnreadableReason.STORE_AWAY]: {
    label: 'Job store away',
    tone: 'warn',
    hint: `The task’s job store did not answer or could not be read. ${KEPT}`,
  },
  // The host tries a broken link again on its own, so the label says what that leaves on screen.
  [UnreadableReason.STREAM_LOST]: {
    label: 'Search stale',
    tone: 'warn',
    hint: `The host’s link to the job service broke, and the host is trying it again. ${KEPT}`,
  },
  [UnreadableReason.MESSAGE_TOO_LARGE]: {
    label: 'Search too large',
    tone: 'warn',
    hint: `The job service sent the host more than the link allows, and reading again cannot fix that. ${KEPT}`,
  },
  [UnreadableReason.NOT_ANSWERING]: {
    label: 'Job service unreachable',
    tone: 'warn',
    hint: `The host asked for the run’s job log and no job service answered. ${KEPT}`,
  },
};

export const unreadableWord = (reason: UnreadableReason): UnreadableWord => UNREADABLE[reason];
