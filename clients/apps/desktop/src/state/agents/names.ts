/**
 * Who an agent is in a run, as data: its role and its number among the agents of that role,
 * counted in the order they started (`planner-2`). The lead is the only agent without a number.
 * The screens turn it into words; nothing here says a role's name.
 */
import {type Agent, AgentRole} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../orchestrator/state.js';
import {ms} from '../orchestrator/time.js';

export interface AgentName {
  readonly role: AgentRole;
  /** 1 for the first agent of the role to start; null for the lead. */
  readonly number: number | null;
}

/** Agents of a run in the order they started, ties broken by id. */
export function startOrder(state: OrchestratorState, runId: string): Agent[] {
  return Object.values(state.agents)
    .filter(agent => agent.runId === runId)
    .sort((a, b) => (ms(a.startedAt) ?? 0) - (ms(b.startedAt) ?? 0) || a.id.localeCompare(b.id));
}

/** The name of every agent of a run, by agent id. */
export function agentNames(
  state: OrchestratorState,
  runId: string,
): ReadonlyMap<string, AgentName> {
  const seen = new Map<AgentRole, number>();
  const names = new Map<string, AgentName>();
  for (const agent of startOrder(state, runId)) {
    const number = (seen.get(agent.role) ?? 0) + 1;
    seen.set(agent.role, number);
    names.set(agent.id, {role: agent.role, number: agent.role === AgentRole.LEAD ? null : number});
  }
  return names;
}
