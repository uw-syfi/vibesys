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
  'kept-input': {kind: 'finished', label: 'Finished · kept the input', tone: 'ok'},
  'no-winner': {kind: 'finished', label: 'No winner', tone: 'neutral'},
  deadline: {kind: 'finished', label: 'Time limit reached', tone: 'warn'},
  'finish-refused': {kind: 'finished', label: 'Not adopted', tone: 'warn'},
  'adoption-failed': {kind: 'failed', label: 'Failed to adopt', tone: 'err'},
  failed: {kind: 'failed', label: 'Failed', tone: 'err'},
  stopped: {kind: 'stopped', label: 'Stopped', tone: 'neutral'},
  force_stopped: {kind: 'stopped', label: 'Force stopped', tone: 'neutral'},
};

/** Words added to an ending that adopted a candidate the input could not be compared with. */
const UNCOMPARED_WORDS = 'adopted without an input comparison';

/**
 * The look of an ending (adopted, the deadline, a stop) whose adopted candidate was never compared
 * with the input, because the input failed its own measurement: never a plain success.
 */
function uncomparedLook(look: EndLook): EndLook {
  return {
    kind: look.kind,
    label: `${look.label} · ${UNCOMPARED_WORDS}`,
    tone: look.tone === 'err' ? 'err' : 'warn',
  };
}

/**
 * An ended run's look: from its end record's outcome, or, on a tombstone that records no end, from
 * its last phase (null when that phase says nothing of an ending).
 */
export function endedLook(run: Pick<HostRun, 'phase' | 'end'>): EndLook | null {
  if (run.end !== null) {
    const look = OUTCOMES[run.end.outcome];
    return run.end.uncompared === true ? uncomparedLook(look) : look;
  }
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
