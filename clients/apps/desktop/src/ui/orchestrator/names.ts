/** People-readable names for agents: `Worker 2`, numbered among the run's agents of that role. */
import {type Agent, AgentRole} from '@vibesys/orchestrator-proto';
import {type OrchestratorState, runAgents} from '../../state/index.js';
import {roleWord} from './words.js';

export function agentNames(state: OrchestratorState, runId: string): ReadonlyMap<string, string> {
  const seen = new Map<AgentRole, number>();
  const names = new Map<string, string>();
  for (const agent of runAgents(state, runId)) {
    const n = (seen.get(agent.role) ?? 0) + 1;
    seen.set(agent.role, n);
    names.set(agent.id, agent.role === AgentRole.LEAD ? 'Lead' : `${roleWord(agent.role)} ${n}`);
  }
  return names;
}

export const nameOf = (names: ReadonlyMap<string, string>, agent: Agent | string): string => {
  const id = typeof agent === 'string' ? agent : agent.id;
  return names.get(id) ?? 'An agent';
};
