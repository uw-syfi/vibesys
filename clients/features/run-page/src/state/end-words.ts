/**
 * How an ended run says what became of it, from facts that are typed rather than from the end
 * record's text: the record's `outcome` (a closed enum) and the run's own trace (which candidate
 * measured best, in which round, how it compares with the input). The record's `reason` is core's
 * account in its own terms (`adopted: settlement:000002/settle.1`), kept for the run's journal and
 * never shown for an ending core words by a code; only an ending whose reason is a person's
 * message (a failure) is shown as it is.
 */
import type {Span} from '@vibesys/contracts';
import type {RunEnd} from '@vibesys/hosts';
import {adoption, bestVsBaseline, gateTrajectory} from '@vibesys/run-state/overview';
import {indexSpans, nearest, numberAttr, ratio, stringAttr} from './span-reading.js';

type Outcome = NonNullable<RunEnd>['outcome'];

const KEPT_INPUT = 'No candidate beat the input, so it was kept as it was.';

/** The candidate a run kept, as its trace says: where it came from and how it compares. */
export interface KeptCandidate {
  /** The round that made it, for a search that plays rounds; null for one that does not. */
  readonly round: number | null;
  /** The hypothesis it tested, when the search names one and no round says where it came from. */
  readonly hypothesis: string | null;
  /** Its value divided by the input's; null when either is unknown. */
  readonly vsBaseline: number | null;
}

/** The workstream of the gate span `gate` (or none), as a candidate that compares by `vsBaseline`. */
function madeBy(
  spans: readonly Span[],
  gate: string | null,
  vsBaseline: number | null,
): KeptCandidate {
  const index = indexSpans(spans);
  const span = gate === null ? undefined : index.byId.get(gate);
  const workstream = span === undefined ? null : nearest(index, span, 'workstream');
  return {
    round: workstream === null ? null : numberAttr(workstream, 'round'),
    hypothesis: workstream === null ? null : stringAttr(workstream, 'hypothesis_id'),
    vsBaseline,
  };
}

/**
 * The candidate the run kept, `'input'` when the input stood, or null when the trace does not say.
 * The search's own account decides (its
 * `adoption` span: the candidate and how it measured, or the input standing); the workstream that
 * made it is the one whose passed gate measured that value. A run whose search labels no adoption
 * is read from its best passed gate.
 */
export function keptCandidate(
  spans: readonly Span[],
  direction: 'max' | 'min' | null | undefined,
): KeptCandidate | 'input' | null {
  const adopted = adoption(spans);
  if (adopted !== null) {
    if (adopted.verdict !== 'adopted') return null;
    if (adopted.candidate === null) return 'input';
    const gate = gateTrajectory(spans).find(
      reading => reading.verdict === 'passed' && reading.metricValue === adopted.metricValue,
    );
    return madeBy(spans, gate?.span ?? null, adopted.vsBaseline);
  }
  const best = bestVsBaseline(spans, direction);
  return best === null ? null : madeBy(spans, best.span, best.vsBaseline);
}

/** How much better than the input the candidate is, in words, or null when it is not known or not. */
function improvement(
  kept: KeptCandidate,
  direction: 'max' | 'min' | null | undefined,
): string | null {
  const {vsBaseline} = kept;
  if (vsBaseline === null || vsBaseline <= 0) return null;
  if (direction === 'min') return vsBaseline < 1 ? `${ratio(1 / vsBaseline)} faster` : null;
  return direction === 'max' && vsBaseline > 1 ? `${ratio(vsBaseline)} faster` : null;
}

function keptWords(
  kept: KeptCandidate | 'input' | null,
  direction: 'max' | 'min' | null | undefined,
): string {
  if (kept === null) return 'Kept the best candidate.';
  if (kept === 'input') return KEPT_INPUT;
  const from =
    kept.round !== null
      ? ` from round ${kept.round}`
      : kept.hypothesis === null
        ? ''
        : ` for ${kept.hypothesis}`;
  const better = improvement(kept, direction);
  return `Kept the candidate${from}${better === null ? '' : `, ${better} than the input`}.`;
}

/**
 * The ended banner's line, for an end record's `outcome` and `reason`. `kept` and `direction` come
 * from the run's trace and declared objective; null when the page has not read them.
 */
export function endReason(
  outcome: Outcome,
  reason: string,
  kept: KeptCandidate | 'input' | null,
  direction: 'max' | 'min' | null | undefined,
): string {
  switch (outcome) {
    case 'adopted':
      return keptWords(kept, direction);
    case 'kept-input':
      return KEPT_INPUT;
    case 'no-winner':
      return 'The run ended without a winner.';
    case 'adoption-failed':
      return 'The run could not adopt its best candidate.';
    case 'finish-refused':
      return 'The run could not record its result.';
    case 'stopped':
      return 'Stopped by you.';
    case 'deadline':
      return 'The time limit was reached.';
    case 'failed':
    case 'force_stopped':
      return reason;
  }
}
