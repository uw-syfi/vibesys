/** The candidate views a client reads; see `index.ts` for how spans become them. */
import type {Span} from '@vibesys/contracts';

type SpanStatus = Span['status'];

/** Where a candidate stands: still worked on, or its own measurement passed or failed it. */
export type CandidateOutcome = 'running' | 'passed' | 'failed';

/** The trusted check that rejected a candidate, and how far it came (`93` of `100` `trials`). */
export interface FailedCheck {
  readonly check: string;
  readonly completed: number | null;
  readonly required: number | null;
  readonly unit: string | null;
}

/** One reading of a measurement: a metric's value with its unit and which way is better. */
export interface MeasuredReading {
  readonly name: string;
  readonly value: number;
  /** The unit the evaluator reported; null when it reported none. */
  readonly unit: string | null;
  readonly direction: 'max' | 'min';
}

/**
 * How the framework compared a candidate's measurement with its base's (`measurement_compared`):
 * the base is the code the candidate was compared against, which is not always the input. Set by
 * whoever joins the run's comparisons to the candidates; null from the spans alone.
 */
export interface BaseComparison {
  /** The candidate's value over its base's. */
  readonly ratio: number;
  /** The commit of the code it was compared with. */
  readonly baseCommit: string;
  /** The base's value as the comparison used it. */
  readonly baseValue: number;
}

/** What a gate of a candidate measured. */
export interface CandidateMeasurement {
  /** The gate span. */
  readonly span: string;
  /** Run-clock seconds when the measurement ended (or started, while it runs). */
  readonly at: number;
  readonly metricValue: number | null;
  /** The candidate's value divided by the input's. */
  readonly vsBaseline: number | null;
  /** The comparison with the idea's base; null until one is joined (see `BaseComparison`). */
  readonly against: BaseComparison | null;
  /** Every reading the benchmark reported, headline first; empty when the gate read none. */
  readonly readings: readonly MeasuredReading[];
  /** The input's own readings, headline first, as this gate knew them; empty while unmeasured. */
  readonly inputReadings: readonly MeasuredReading[];
  /** `passed`, `failed_check` or `slower` once the search decided; null before. */
  readonly verdict: string | null;
  /** The first trusted check that rejected the candidate; null when every check passed. */
  readonly failed: FailedCheck | null;
}

/** One labelled span of an attempt: an agent turn, a gate, or a kind this client does not know. */
export interface CandidateStep {
  readonly span: string;
  /** The label's kind: `agent_turn`, `gate`, … */
  readonly kind: string;
  /** The label's title, else the span's name. */
  readonly title: string;
  /** The role an agent turn asked; null for any other step. */
  readonly role: string | null;
  readonly status: SpanStatus;
  readonly startedAt: number;
  readonly endedAt: number | null;
}

/** One attempt at a candidate: one workstream span and its steps. */
export interface CandidateAttempt {
  /** The workstream span. */
  readonly span: string;
  readonly title: string;
  readonly idea: string | null;
  /** The round that ran it, for a search that plays rounds. */
  readonly round: number | null;
  readonly outcome: CandidateOutcome;
  /** Why it failed (`turned_down`, `not_measured`, `no_design`, `not_opened`), when the span says. */
  readonly reason: string | null;
  /** The revision it kept; null while it works and when it kept nothing. */
  readonly revision: string | null;
  /** The round of the idea whose code it started from; null: the input. */
  readonly baseRound: number | null;
  readonly startedAt: number;
  readonly endedAt: number | null;
  /** Its latest gate that measured a value or reached a verdict. */
  readonly measurement: CandidateMeasurement | null;
  /** Its steps, in the order they started. */
  readonly steps: readonly CandidateStep[];
}

/** One candidate: an idea and the code its attempts wrote. */
export interface Candidate {
  /** Stable: the span of its first attempt. */
  readonly id: string;
  /** The idea's identity the search gave it; null while its only attempt has none yet. */
  readonly hypothesis: string | null;
  /** The candidate it built on; null for the input. Always an earlier candidate. */
  readonly parent: string | null;
  readonly title: string;
  /** What it tries, in an agent's own words; null until an agent said it. */
  readonly idea: string | null;
  readonly round: number | null;
  /** Running while an attempt runs; else passed when an attempt passed; else failed. */
  readonly outcome: CandidateOutcome;
  /** Why it failed; null unless it failed and the span says. */
  readonly reason: string | null;
  /** The round of the idea its first attempt started from; null: the input. */
  readonly baseRound: number | null;
  /**
   * What it measured: a passed candidate's latest measured passed attempt, else its latest measured
   * attempt; null when nothing was measured.
   */
  readonly measurement: CandidateMeasurement | null;
  /** Its attempts, in the order they started. */
  readonly attempts: readonly CandidateAttempt[];
  readonly startedAt: number;
  /** When its last attempt's span ended; null while one runs or a span is still open. */
  readonly endedAt: number | null;
  /** The steps running now, in any attempt: where a running row jumps to. */
  readonly running: readonly CandidateStep[];
}

/**
 * The candidate the run's adoption span names: the best so far while the run works, the adopted
 * result once it ended (the search updates the span's candidate as the best changes).
 */
export interface BestCandidate {
  /** The candidate; null when the input itself stands. */
  readonly candidate: string | null;
  /** The run ended and adopted it; false while the run works and the best may still change. */
  readonly adopted: boolean;
}

/** The run's candidates and the best of them. */
export interface CandidateTree {
  /** In the order they began, so every candidate comes after its parent. */
  readonly candidates: readonly Candidate[];
  /** Null before the run labels an adoption, and when the adoption failed. */
  readonly best: BestCandidate | null;
  /**
   * Whether the run has an adoption span at all. It opens when the run ends, so while the run works
   * this is false and `best` is null; a reader that shows the best so far must fill it in itself.
   */
  readonly adoptionOpened: boolean;
  /**
   * The winning path: the candidates from the input to the best, each built on the one before
   * (the best last); empty while the input stands or nothing is best.
   */
  readonly bestPath: readonly string[];
}
