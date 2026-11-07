/**
 * What a client knows about one host's orchestrator, as plain data. Every record is the latest
 * version of one fact, replaced by id: the daemon sends whole facts, so there is nothing to merge
 * (docs: `proto/vibesys/orchestrator/v1/events.proto`). Selectors read it; only `fold.ts` writes it.
 */
import type {
  Agent,
  CheckIn,
  Item,
  Message,
  ModelCatalog,
  Receipt,
  Run,
  RunSummary,
  SearchState,
  Turn,
} from '@vibesys/orchestrator-proto';
import type {CostView} from './cost.js';

/** One piece of progress on a running item, with the sequence it was made at. */
export interface Progress {
  readonly sequence: bigint;
  readonly text: string;
  readonly commandOutput: string;
}

/** Progress on a running item that its next full update has not yet replaced. */
export interface Overlay {
  readonly agentId: string;
  /** Text added since the item was last stored. */
  readonly text: string;
  /** Command output added since the item was last stored. */
  readonly commandOutput: string;
  /** The pieces `text` and `commandOutput` join, kept so a newer item can drop the ones it holds. */
  readonly pieces: readonly Progress[];
}

/** The stream skipped events: the client must read again from a snapshot. */
export interface Gap {
  readonly expected: bigint;
  readonly got: bigint;
}

/**
 * What the host left out of the last snapshot, as it said: whether it cut anything to fit its byte
 * budget, and how many whole runs it did not list (the oldest finished ones).
 */
export interface SnapshotCut {
  readonly truncated: boolean;
  readonly runsLeftOut: number;
}

/**
 * That the history of a run (its agents, turns, messages and check-ins) was read on request
 * (`GetRunHistory`), with the sequence the first page was current to. A run's history is not in the
 * snapshot, so a screen of the run reads it; a new snapshot forgets that it was read.
 */
export interface RunHistoryRead {
  readonly sequence: bigint;
  /** The last page was folded: the history is whole, but for what the host says it left out. */
  readonly complete: boolean;
  /** The run has more turns than the host's timeline window, and the later ones were not served. */
  readonly turnsLeftOut: boolean;
  /** A viewer made more check-ins than the host serves, and the older ones were not served. */
  readonly checkInsLeftOut: boolean;
}

export interface OrchestratorState {
  /** The last event applied, or the sequence of the snapshot; 0n before any. */
  readonly sequence: bigint;
  /** Revision floors for the current WatchRuns stream, separate from retained display facts. */
  readonly searchRevisions: Readonly<Record<string, bigint>>;
  readonly searches: Readonly<Record<string, SearchState>>;
  /** The latest cost view of each run whose cost stream a screen has watched, by run id. */
  readonly costs: Readonly<Record<string, CostView>>;
  readonly runs: Readonly<Record<string, Run>>;
  /**
   * The summary the host serves of each run it listed, replaced whole by run id: the report that sums
   * the run up, the agents working and the head of each objective. Screens show these and derive none.
   */
  readonly summaries: Readonly<Record<string, RunSummary>>;
  /** What the last snapshot left out. */
  readonly cut: SnapshotCut;
  /**
   * The models the host runs agents on and allows, as its last snapshot said: what the Start run form
   * offers. Null until a snapshot has said, and for a host older than the catalog.
   */
  readonly models: ModelCatalog | null;
  /** The runs whose history was read, by run id. */
  readonly histories: Readonly<Record<string, RunHistoryRead>>;
  readonly agents: Readonly<Record<string, Agent>>;
  readonly turns: Readonly<Record<string, Turn>>;
  readonly items: Readonly<Record<string, Item>>;
  /**
   * Items a page left out the large text of (reasoning, tool output), by item id, with how many bytes
   * were left out. Reading the item whole (`GetItem`) or a live frame of it clears the entry.
   */
  readonly elided: Readonly<Record<string, number>>;
  /** The sequence each stored item was committed at: the revision that decides which copy is newer. */
  readonly itemSequences: Readonly<Record<string, bigint>>;
  readonly messages: Readonly<Record<string, Message>>;
  /**
   * Every viewer's check-ins of the runs held (Catch me up), by check-in id, each whole with the facts
   * the host froze in it. A viewer's own list is the ones with its id (`rewind` in `check-ins.ts`).
   */
  readonly checkIns: Readonly<Record<string, CheckIn>>;
  /** Answers to commands this client has seen, by command id. */
  readonly receipts: Readonly<Record<string, Receipt>>;
  /** Unstored progress, by item id. */
  readonly overlays: Readonly<Record<string, Overlay>>;
  readonly gap: Gap | null;
}

export const emptyState: OrchestratorState = {
  sequence: 0n,
  searches: {},
  costs: {},
  searchRevisions: {},
  runs: {},
  summaries: {},
  cut: {truncated: false, runsLeftOut: 0},
  models: null,
  histories: {},
  agents: {},
  turns: {},
  items: {},
  elided: {},
  itemSequences: {},
  messages: {},
  checkIns: {},
  receipts: {},
  overlays: {},
  gap: null,
};
