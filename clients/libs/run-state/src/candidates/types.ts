/** The candidate views a client reads; see `index.ts` for how spans become them. */
import type {Span} from '@vibesys/contracts';

type SpanStatus = Span['status'];

/** Where a candidate stands: still worked on, its code kept, or not kept. */
export type CandidateOutcome = 'running' | 'kept' | 'rejected';

/** The trusted check that rejected a candidate, and how far it came (`93` of `100` `trials`). */
export interface FailedCheck {
  readonly check: string;
  readonly completed: number | null;
  readonly required: number | null;
  readonly unit: string | null;
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
  /** Why it was not kept, when the search or the span says. */
  readonly reason: string | null;
  /** The revision it kept; null while it works and when it kept nothing. */
  readonly revision: string | null;
  readonly startedAt: number;
  readonly endedAt: number | null;
  /** Its latest gate that measured a value or reached a verdict. */
  readonly measurement: CandidateMeasurement | null;
  /** The values its passed gates measured, in the order the gates started. */
  readonly passed: readonly number[];
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
  /** Running while an attempt runs; else kept when an attempt kept its code; else rejected. */
  readonly outcome: CandidateOutcome;
  readonly reason: string | null;
  /**
   * What it measured: a kept candidate's latest measured kept attempt, else its latest measured
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

/** The candidate the run stands on: the one it adopted, or (before adopting) the best measured. */
export interface BestCandidate {
  /** The candidate; null when the input itself stands. */
  readonly candidate: string | null;
  /** The run adopted it (or is adopting it); false for the best passed measurement so far. */
  readonly adopted: boolean;
}

/** The run's candidates and the best of them. */
export interface CandidateTree {
  /** In the order they began, so every candidate comes after its parent. */
  readonly candidates: readonly Candidate[];
  /** Null while nothing passed and nothing was adopted, and when the adoption failed. */
  readonly best: BestCandidate | null;
  /**
   * The winning path: the candidates from the input to the best, each built on the one before
   * (the best last); empty while the input stands or nothing is best.
   */
  readonly bestPath: readonly string[];
}
