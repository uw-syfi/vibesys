/**
 * How an ended run reads, from its registry record: one table of outcomes, shared by the sidebar's
 * rows and the Run screen's header so they never disagree. A run that ended without a winner, or
 * without adopting one, is not shown as a success.
 */
import type {HostRun} from './listing.js';

/** The tone of an end: success is `ok`; an ending that is not a failure but not a success is `neutral` or `warn`. */
export type EndTone = 'ok' | 'warn' | 'err' | 'neutral';

/** What kind of ending it was, in the sidebar's three groups. */
export type EndKind = 'finished' | 'failed' | 'stopped';

export interface EndLook {
  readonly kind: EndKind;
  /** The words, capitalized, naming the outcome. */
  readonly label: string;
  readonly tone: EndTone;
}

type Outcome = NonNullable<HostRun['end']>['outcome'];

const OUTCOMES: Readonly<Record<Outcome, EndLook>> = {
  adopted: {kind: 'finished', label: 'Finished', tone: 'ok'},
  // A candidate passed the gates, but the input it should beat failed its own measurement.
  'adopted-without-input-comparison': {
    kind: 'finished',
    label: 'Finished · adopted without an input comparison',
    tone: 'warn',
  },
  'kept-input': {kind: 'finished', label: 'Finished · kept the input', tone: 'ok'},
  'no-winner': {kind: 'finished', label: 'No winner', tone: 'neutral'},
  deadline: {kind: 'finished', label: 'Time limit reached', tone: 'warn'},
  'finish-refused': {kind: 'finished', label: 'Not adopted', tone: 'warn'},
  'adoption-failed': {kind: 'failed', label: 'Failed to adopt', tone: 'err'},
  failed: {kind: 'failed', label: 'Failed', tone: 'err'},
  stopped: {kind: 'stopped', label: 'Stopped', tone: 'neutral'},
  force_stopped: {kind: 'stopped', label: 'Force stopped', tone: 'neutral'},
};

/**
 * An ended run's look: from its end record's outcome, or, on a tombstone that records no end, from
 * its last phase (null when that phase says nothing of an ending).
 */
export function endedLook(run: Pick<HostRun, 'phase' | 'end'>): EndLook | null {
  if (run.end !== null) return OUTCOMES[run.end.outcome];
  switch (run.phase) {
    case 'completed':
      return {kind: 'finished', label: 'Finished', tone: 'ok'};
    case 'failed':
      return OUTCOMES.failed;
    case 'stopped':
      return OUTCOMES.stopped;
    default:
      return null;
  }
}
