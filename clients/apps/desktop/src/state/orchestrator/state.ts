/**
 * What a client knows about one host's orchestrator, as plain data. Every record is the latest
 * version of one fact, replaced by id: the daemon sends whole facts, so there is nothing to merge
 * (docs: `proto/vibesys/orchestrator/v1/events.proto`). Selectors read it; only `fold.ts` writes it.
 */
import type {
  Agent,
  Item,
  Message,
  Receipt,
  Run,
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

export interface OrchestratorState {
  /** The last event applied, or the sequence of the snapshot; 0n before any. */
  readonly sequence: bigint;
  /** Revision floors for the current WatchRuns stream, separate from retained display facts. */
  readonly searchRevisions: Readonly<Record<string, bigint>>;
  readonly searches: Readonly<Record<string, SearchState>>;
  /** The latest cost view of each run whose cost stream a screen has watched, by run id. */
  readonly costs: Readonly<Record<string, CostView>>;
  readonly runs: Readonly<Record<string, Run>>;
  readonly agents: Readonly<Record<string, Agent>>;
  readonly turns: Readonly<Record<string, Turn>>;
  readonly items: Readonly<Record<string, Item>>;
  /** The sequence each stored item was committed at: the revision that decides which copy is newer. */
  readonly itemSequences: Readonly<Record<string, bigint>>;
  readonly messages: Readonly<Record<string, Message>>;
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
  agents: {},
  turns: {},
  items: {},
  itemSequences: {},
  messages: {},
  receipts: {},
  overlays: {},
  gap: null,
};
