/**
 * The run's candidates: each idea an agent tried plus the code it wrote, as a tree of what each
 * built on, rooted at the run's input, with the best candidate and the winning path to it. Folded
 * from the run's span events (`CandidateFold`, linear in events) or from spans (`candidateTree`);
 * there is no second record of the run's work.
 *
 * It reads the span kinds every hypothesis search labels (`vibesys.search_spans`), the same way
 * whatever the search:
 * - a `workstream` span is one attempt at a candidate; attempts at one idea share its
 *   `hypothesis_id` (an attempt without one yet is a candidate of its own); a `probe` only looks
 *   and is no candidate;
 * - its `builds_on` link names the workstream, so the candidate, it built on; none is the input;
 * - its verdict says whether the attempt passed or failed, and `revision` names the code it kept,
 *   which the run's `adoption` span names while it is the best and when the run adopts it;
 * - the labelled spans under it (agent turns, gates) are its steps, and its gates what it measured.
 *
 * A search that plays one path links each round to the round whose code it started from (its
 * `base_round`), so its tree is a chain whose failed rounds are leaves.
 *
 * Spans carry no agent-spawn relation (which agent started which): an agent turn's span parent is
 * the flow task that awaited it, so a candidate's steps say which attempt ran a turn, not who
 * spawned the agent.
 */
import type {Span} from '@vibesys/contracts';
import {CandidateFold} from './fold.js';
import type {Candidate, CandidateTree} from './types.js';

export {CandidateFold, type WorkMeter} from './fold.js';
export {
  candidateLineage,
  type Lineage,
  type SubtreeStats,
} from './lineage.js';
export {
  type ObjectiveAxis,
  type Tradeoff,
  type TradeoffAxis,
  type TradeoffPoint,
  tradeoffOf,
} from './tradeoff.js';
export type {
  BaseComparison,
  BestCandidate,
  Candidate,
  CandidateAttempt,
  CandidateMeasurement,
  CandidateOutcome,
  CandidateStep,
  CandidateTree,
  FailedCheck,
  MeasuredReading,
} from './types.js';

/** The candidate tree of the run whose spans these are, in the order they started. */
export function candidateTree(spans: readonly Span[]): CandidateTree {
  const fold = new CandidateFold();
  fold.applySpans(spans);
  return fold.tree();
}

/** The candidates that built on `parent` (null: on the input), in the order they began. */
export function candidateChildren(tree: CandidateTree, parent: string | null): Candidate[] {
  return tree.candidates.filter(item => item.parent === parent);
}
