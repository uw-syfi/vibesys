/**
 * What an agent is called on screen: one rule for every screen. The name is the role and the number
 * among the agents of that role in the order they started (`lead`, `worker-2`), the same as the
 * agents' tree and transcripts say it, from the same numbering (`agentNames` of the state). An id
 * that names no agent of the run is never shown: it reads `An agent`.
 */
import type {Agent} from '@vibesys/orchestrator-proto';
import {agentNames as numbered, type OrchestratorState} from '../../state/index.js';
import {agentLabel} from '../agents/labels.js';

export function agentNames(state: OrchestratorState, runId: string): ReadonlyMap<string, string> {
  return new Map([...numbered(state, runId)].map(([id, name]) => [id, agentLabel(name)] as const));
}

export const nameOf = (names: ReadonlyMap<string, string>, agent: Agent | string): string => {
  const id = typeof agent === 'string' ? agent : agent.id;
  return names.get(id) ?? 'An agent';
};
