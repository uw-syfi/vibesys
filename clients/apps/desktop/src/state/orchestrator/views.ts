/**
 * Reads of the state for the screens: a run's agents, an agent's turns, steps and messages. Pure functions of the state and, where a running thing has no end
 * yet, of `nowMs`, which the caller passes (no clock here).
 */
import type {Agent, Message, Turn} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from './state.js';
import {ms} from './time.js';

const startOf = (agent: Agent): number => ms(agent.startedAt) ?? 0;

export function runAgents(state: OrchestratorState, runId: string): Agent[] {
  return Object.values(state.agents)
    .filter(agent => agent.runId === runId)
    .sort((a, b) => startOf(a) - startOf(b) || a.id.localeCompare(b.id));
}

export function agentTurns(state: OrchestratorState, agentId: string): Turn[] {
  return Object.values(state.turns)
    .filter(turn => turn.agentId === agentId)
    .sort((a, b) => a.ordinal - b.ordinal);
}

/**
 * Where the newest unbroken run of an agent's loaded items starts, or null when none is loaded.
 * Reading older items below it first fills any hole left while the screen was away, then goes on
 * to the beginning, so one "Show earlier steps" action reaches every step.
 */
export function unbrokenFrom(state: OrchestratorState, agentId: string): number | null {
  const ordinals = new Set(
    Object.values(state.items)
      .filter(item => item.agentId === agentId)
      .map(item => item.ordinal),
  );
  if (ordinals.size === 0) return null;
  let from = Math.max(...ordinals);
  while (ordinals.has(from - 1)) from -= 1;
  return from;
}

/**
 * Whether any step of the agent is held, from its stream or read on request: what its screen
 * shows, whatever the stream's cursor says.
 */
export function holdsSteps(state: OrchestratorState, agentId: string): boolean {
  return Object.values(state.items).some(item => item.agentId === agentId);
}

/** Messages to or from an agent, oldest first. */
export function messagesOf(state: OrchestratorState, agentId: string): Message[] {
  return Object.values(state.messages)
    .filter(message => message.toAgentId === agentId || message.fromAgentId === agentId)
    .sort((a, b) => (ms(a.at) ?? 0) - (ms(b.at) ?? 0) || a.id.localeCompare(b.id));
}
