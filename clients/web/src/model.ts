/** View models: `derive.ts` builds them from store state, the `ui/` components render them. */
import type {DesignFileChange} from '@vibesys/backend-client/browser';
import type {AgentPhaseStatus, CoreRunStatus, RoundSummary} from '@vibesys/core-state';

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

/** A point of the rail's sparkline, in the `0 0 100 36` user space of its viewBox. */
export interface TrendPoint {
  x: number;
  y: number;
}

/** The shape of the metric across rounds, under the rail rows. Null below two points. */
export interface TrendModel {
  /** Two or more points in round order; the last one carries the dot. */
  points: TrendPoint[];
  /** First and last plotted values, compact, as the rail rows write them. */
  first: string;
  last: string;
  /** The rounds those two values belong to: the span the curve covers. */
  firstRound: number;
  lastRound: number;
}

/** How the arrow between two agents of the graph is toned. */
export type EdgeTone = 'idle' | 'done' | 'live' | 'failed';

/** A card in the agent graph: one agent, or the run of adjacent agents that say the same thing. */
export interface GraphNode {
  /** The first agent's execution id, or its kind and row while it is still pending. */
  id: string;
  /** Title-cased agent kind, e.g. `Implementer`. */
  role: string;
  status: AgentPhaseStatus;
  /** The model id, or the harness when no model was recorded; null when neither was. */
  runtime: string | null;
  /** `Claude Code (claude-opus-5)` when the card shows only half of it; null when it shows all. */
  runtimeTip: string | null;
  /** Adjacent agents of the kind sharing this status and runtime; 1 for a lone agent. */
  count: number;
}

/** A handover, from one node's id to another's, toned by its two ends. */
export interface GraphEdge {
  from: string;
  to: string;
  tone: EdgeTone;
}

/** The round's agents and the handovers between them. Nodes are in loop order. */
export interface AgentGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/** A node in the box the layout gave it: its top-left corner, in CSS pixels. */
export interface PlacedNode extends GraphNode {
  x: number;
  y: number;
}

/** An edge on the polyline the layout routed it along, source border to target border. */
export interface PlacedEdge extends GraphEdge {
  points: Array<{x: number; y: number}>;
}

/** A laid-out graph and the canvas it needs. */
export interface GraphLayout {
  width: number;
  height: number;
  nodes: PlacedNode[];
  edges: PlacedEdge[];
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
  | LogTool
  | {kind: 'steer'; id: string; text: string}
  /**
   * Adjacent calls of one verb, read as one counted row that opens in place. A client
   * heuristic, not a fold the producer declared: the protocol carries no fold level.
   */
  | {kind: 'run'; id: string; verb: string; items: LogTool[]};

export interface LogTool {
  kind: 'tool';
  id: string;
  verb: string;
  /** What the row shows: for a command, the executable and the first path it names. */
  arg: string | null;
  /** The whole command, when `arg` is a reduction of it; null when the row shows all of it. */
  argFull: string | null;
  result: ToolResultSummary | null;
  /** The call's wall clock, which only a command payload reports; null when it did not. */
  duration: string | null;
  inFlight: boolean;
}

export interface LogGroup {
  id: string;
  role: string;
  /** Which attempt made these rows; past 1 the header badges it. */
  attempt: number;
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
