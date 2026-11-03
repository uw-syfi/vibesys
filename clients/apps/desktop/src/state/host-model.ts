/**
 * Everything a client knows about one host's orchestrator: the folded state of both streams, and
 * how far each agent's stream has been applied. The cursor lives inside the value, so the data and
 * the cursor can only change together.
 */
import type {WatchRunsResponse} from '@vibesys/orchestrator-proto';
import {type AgentFrame, applyAgentFeed, applyEvent, applySnapshot} from './orchestrator/fold.js';
import {emptyState, type OrchestratorState} from './orchestrator/state.js';

export interface HostModel {
  readonly data: OrchestratorState;
  /** The newest sequence applied from each agent's stream, by agent id. */
  readonly agentCursors: Readonly<Record<string, bigint>>;
}

/** One frame of either stream, tagged with where it came from. */
export type HostFrame =
  | {readonly from: 'runs'; readonly body: WatchRunsResponse['body']}
  | {readonly from: 'agent'; readonly agentId: string; readonly frame: AgentFrame};

export const emptyHostModel: HostModel = {data: emptyState, agentCursors: {}};

/**
 * Fold one frame. Runs and agents fold into one state, so the two streams' copies of an item
 * reconcile by sequence (`fold.ts`).
 */
export function applyHostFrame(model: HostModel, frame: HostFrame): HostModel {
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
    case 'event':
      return {...model, data: applyEvent(model.data, body.value)};
    default:
      return model;
  }
}

/** Where a new run stream starts: after the last applied event, or from a snapshot when behind. */
export function runsCursor(model: HostModel): bigint {
  return model.data.gap === null ? model.data.sequence : 0n;
}
