/**
 * The agents of a run by type, as the host counts them (`RunSummary`): how many the run has had and
 * how many are working now. The lead spawns agents freely, so there is no fixed set of roles: a type
 * with no agent is not served. The host owns what working means (starting, in a turn, or waiting on
 * subagents), so this screen and the run list can never disagree on it.
 */
import type {AgentRole} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../state.js';

export interface AgentTypeCount {
  readonly role: AgentRole;
  /** Agents of the type the run has had, over or not. */
  readonly total: number;
  /** Agents of the type that are working, as the host counts them. */
  readonly running: number;
}

export interface AgentsStrip {
  /** In the order the host serves them. */
  readonly types: readonly AgentTypeCount[];
  readonly total: number;
  readonly running: number;
}

const NONE: AgentsStrip = {types: [], total: 0, running: 0};

/** The agents of the run as the host served them; none before it sent the run's summary. */
export function agentsStrip(state: OrchestratorState, runId: string): AgentsStrip {
  const summary = state.summaries[runId];
  if (summary === undefined) return NONE;
  return {
    types: summary.roles.map(({role, total, working}) => ({role, total, running: working})),
    total: summary.agentsTotal,
    running: summary.agentsWorking,
  };
}
