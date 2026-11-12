/**
 * Everything a client knows about one host's orchestrator: the folded state of its streams (the
 * runs, each agent's and each run's cost), and how far each agent's stream has been applied. The
 * cursor lives inside the value, so the data and the cursor can only change together.
 */
import type {WatchRunsResponse} from '@vibesys/orchestrator-proto';
import type {CostView} from './orchestrator/cost.js';
import {
  type AgentFrame,
  applyAgentFeed,
  applyChange,
  applyCost,
  applyHostCost,
  applyRunHistory,
  applySearch,
  applySnapshot,
  applySummary,
  type RunHistoryFrame,
  replaceCost,
  replaceHostCost,
  type WholeViewPhase,
} from './orchestrator/fold.js';
import type {HostCostView, HostCostWindow} from './orchestrator/host-cost.js';
import {emptyState, type OrchestratorState} from './orchestrator/state.js';

export interface HostModel {
  readonly data: OrchestratorState;
  /** The newest sequence applied from each agent's stream, by agent id. */
  readonly agentCursors: Readonly<Record<string, bigint>>;
}

/** One frame of any of the streams, tagged with where it came from. */
export type HostFrame =
  | {readonly from: 'runs'; readonly body: WatchRunsResponse['body']}
  | {readonly from: 'agent'; readonly agentId: string; readonly frame: AgentFrame}
  /**
   * An answer to a request about an agent's past (an older page, one item whole). It folds like an
   * agent frame but says nothing about where the agent's stream is, so no cursor moves.
   */
  | {readonly from: 'history'; readonly agentId: string; readonly frame: AgentFrame}
  /** A run's whole cost view, as the host works it out, already read from its message. */
  | {readonly from: 'cost'; readonly phase: WholeViewPhase; readonly view: CostView}
  /** The whole host's cost view in one window, as the host works it out, already read from its message. */
  | {
      readonly from: 'host-cost';
      readonly phase: WholeViewPhase;
      readonly window: HostCostWindow;
      readonly view: HostCostView;
    }
  /**
   * One page of a run's history (its agents, turns, messages and check-ins), read on request: a
   * snapshot of runs holds none, and the run stream's cursor does not move for it.
   */
  | ({readonly from: 'run-history'} & RunHistoryFrame);

export const emptyHostModel: HostModel = {data: emptyState, agentCursors: {}};

/**
 * Fold one frame. Runs and agents fold into one state, so the two streams' copies of an item
 * reconcile by sequence (`fold.ts`); a cost view is a whole fact of its own.
 */
export function applyHostFrame(model: HostModel, frame: HostFrame): HostModel {
  if (frame.from === 'history') return {...model, data: applyAgentFeed(model.data, frame.frame)};
  if (frame.from === 'cost') {
    const data =
      frame.phase === 'initial'
        ? replaceCost(model.data, frame.view)
        : applyCost(model.data, frame.view);
    return {...model, data};
  }
  if (frame.from === 'host-cost')
    return {
      ...model,
      data:
        frame.phase === 'initial'
          ? replaceHostCost(model.data, frame.window, frame.view)
          : applyHostCost(model.data, frame.window, frame.view),
    };
  if (frame.from === 'run-history') return {...model, data: applyRunHistory(model.data, frame)};
  if (frame.from === 'agent') {
    const before = model.agentCursors[frame.agentId] ?? 0n;
    const at = frame.frame.sequence > before ? frame.frame.sequence : before;
    return {
      data: applyAgentFeed(model.data, frame.frame),
      agentCursors: {...model.agentCursors, [frame.agentId]: at},
    };
  }
  const {body} = frame;
  switch (body.case) {
    case 'snapshot':
      return {...model, data: applySnapshot(model.data, body.value)};
    case 'search':
      return {...model, data: applySearch(model.data, body.value)};
    case 'summary':
      return {...model, data: applySummary(model.data, body.value)};
    case 'change':
      return {...model, data: applyChange(model.data, body.value)};
    case undefined:
      return model;
    default:
      body satisfies never;
      return model;
  }
}

/** Where a new run stream starts: after the last applied event, or from a snapshot when behind. */
export function runsCursor(model: HostModel): bigint {
  return model.data.gap === null ? model.data.sequence : 0n;
}

/** Start a revision scope without erasing facts while the new stream is still opening. */
export function beginRunsStream(model: HostModel): HostModel {
  return {...model, data: {...model.data, searchRevisions: {}}};
}

/**
 * The model once the host said its log ends before the run stream's cursor: what it holds stays on
 * screen, and the next open reads a snapshot that replaces it (`applySnapshot` takes the read that
 * closes a gap as authoritative, whatever its sequence).
 */
export function rewindRunsStream(model: HostModel): HostModel {
  const gap = {expected: model.data.sequence + 1n, got: null};
  return {...model, data: {...model.data, gap}};
}

/**
 * The model once the host said its log ends before `agentId`'s stream cursor: the cursor is
 * forgotten, so the next open reads the agent's newest page again.
 */
export function rewindAgentStream(model: HostModel, agentId: string): HostModel {
  const {[agentId]: _forgotten, ...agentCursors} = model.agentCursors;
  return {...model, agentCursors};
}
