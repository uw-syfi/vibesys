/**
 * The agents of a run by type: how many the run has had and how many are working a turn now. The
 * lead spawns agents freely, so there is no fixed set of roles: a type with no agent is left out.
 */
import {AgentRole, AgentStatus} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../state.js';

export interface AgentTypeCount {
  readonly role: AgentRole;
  /** Agents of the type the run has had, over or not. */
  readonly total: number;
  /** Agents of the type whose session is opening or in a turn. */
  readonly running: number;
}

export interface AgentsStrip {
  /** In the order of the contract's roles. */
  readonly types: readonly AgentTypeCount[];
  readonly total: number;
  readonly running: number;
}

const RUNNING = new Set<AgentStatus>([AgentStatus.LAUNCHING, AgentStatus.ACTIVE]);

const ROLES: readonly AgentRole[] = [
  AgentRole.LEAD,
  AgentRole.PLANNER,
  AgentRole.WORKER,
  AgentRole.JUDGE,
  AgentRole.COMBINER,
  AgentRole.PROFILER,
  AgentRole.UNSPECIFIED,
];

export function agentsStrip(state: OrchestratorState, runId: string): AgentsStrip {
  const agents = Object.values(state.agents).filter(agent => agent.runId === runId);
  const types = ROLES.flatMap(role => {
    const ofType = agents.filter(agent => agent.role === role);
    return ofType.length === 0
      ? []
      : [
          {
            role,
            total: ofType.length,
            running: ofType.filter(agent => RUNNING.has(agent.status)).length,
          },
        ];
  });
  return {
    types,
    total: agents.length,
    running: types.reduce((sum, type) => sum + type.running, 0),
  };
}
