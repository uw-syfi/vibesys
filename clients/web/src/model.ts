/** View models: `derive.ts` builds them from store state, the `ui/` components render them. */
import type {DesignFileChange} from '@vibesys/backend-client/browser';
import type {CoreRunStatus, RoundSummary} from '@vibesys/core-state';

export type Connection = 'connecting' | 'connected' | 'disconnected' | 'error';

/**
 * `baseline` is R0, present when the objective records a baseline value. `failed` is a failed
 * gate. `running` and `paused` only ever mark the live round.
 */
export type RoundStatus = 'baseline' | 'kept' | 'rejected' | 'failed' | 'running' | 'paused';

export interface RailRow {
  round: number;
  status: RoundStatus;
  /** Compact headline value such as `112.7M`; null when the round recorded none. */
  value: string | null;
  /** Absolute value with its unit, for the tooltip. */
  valueTip: string | null;
  official: boolean;
  incumbent: boolean;
  /** Timing of the running or paused row, which shows round elapsed instead of a value. */
  live: RoundSummary | null;
}

/** `error` is a failed first load: the rail shows only the error, no skeleton. */
export type RailState = 'loading' | 'error' | 'unattached' | 'ready';

export interface RailModel {
  rows: RailRow[];
  /** `max_rounds` minus the latest started round; null when unknown or the run ended. */
  roundsLeft: number | null;
}

export interface ProsePart {
  kind: 'text' | 'code' | 'strong';
  text: string;
}

export interface ToolResultSummary {
  text: string;
  failed: boolean;
}

export type LogItem =
  | {kind: 'prose'; id: string; paragraphs: ProsePart[][]}
  | {
      kind: 'tool';
      id: string;
      verb: string;
      arg: string | null;
      result: ToolResultSummary | null;
      inFlight: boolean;
    }
  | {kind: 'steer'; id: string; text: string};

export interface LogGroup {
  id: string;
  role: string;
  attempt: number;
  /** Set on the first group of a retry: render an "Attempt N" divider before it. */
  divider: number | null;
  collapsed: boolean;
  /** The role that is acting right now; its label renders bright. */
  active: boolean;
  summary: string;
  calls: number;
  items: LogItem[];
}

export type Verdict = 'Gate failed' | 'Rejected' | 'Kept' | 'Passed';

export interface JudgeAttempt {
  attempt: number;
  verdict: Verdict;
  feedback: string;
  open: boolean;
}

export interface InspectorModel {
  round: number;
  hypothesis: {id: string | null; title: string | null; claim: string | null} | null;
  metric: {name: string; direction: 'max' | 'min' | null} | null;
  /** `value` null reads "Pending". `vs` is the incumbent round compared against. */
  delta: {value: string | null; vs: number | null; tip: string | null} | null;
  judge: JudgeAttempt[];
  /** Finished rounds whose commit range resolved only. */
  changes: {commit: string | null; files: DesignFileChange[]} | null;
}

export type EndedWord = 'Completed' | 'Failed' | 'Interrupted';

export type RunControl =
  | {
      kind: 'action';
      action: 'pause' | 'resume';
      label: 'Pause' | 'Pausing' | 'Resume';
      tip: string;
      disabled: boolean;
    }
  | {kind: 'ended'; word: EndedWord; tip: string | null};

export interface HeaderModel {
  /** Basename of `run_started.input`. */
  project: string | null;
  /** First sentence and full text of `performance_context.objective_description`. */
  objective: {first: string; full: string} | null;
  startedAt: string | null;
  endedAt: string | null;
  control: RunControl;
}

export interface Steers {
  /** Steers the backend journaled as pending and has not consumed yet. */
  pending: PendingSteer[];
  consumed: ConsumedSteer[];
}

export interface PendingSteer {
  /** `steer-<sequence of the pending control event>`. */
  id: string;
  text: string;
}

export interface ConsumedSteer extends PendingSteer {
  /** Sequence of the `control` consumed event: the steer's position in the log. */
  sequence: number;
  round: number | null;
  roundLabel: string | null;
  agentKind: string | null;
}

/** What the live region compares between renders. */
export interface RunPulse {
  status: CoreRunStatus;
  round: number | null;
  ended: EndedWord | null;
  connection: Connection;
}
