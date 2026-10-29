/**
 * An agent's turns on the Agents face: the `agent_turn` spans whose `agent` attribute names it, in
 * the order they started, each with its headline (the label's title) and when it ran. The agent
 * graph (`@vibesys/run-state/agents`) keeps only an agent's latest turn; the pane that lists every
 * turn reads the trace here, once per agent opened.
 */
import {declaredRoleName, type Span} from '@vibesys/contracts';
import type {CoreState} from '@vibesys/run-state';
import type {AgentNode} from '@vibesys/run-state/agents';
import {stringAttr} from './span-reading.js';

/** One turn of an agent. */
export interface AgentTurn {
  /** The turn's span: what `open-step` opens. */
  readonly span: string;
  /** 1 for the agent's first turn. */
  readonly number: number;
  /** What the turn says it does: the label's title; null when the label has none. */
  readonly headline: string | null;
  readonly status: Span['status'];
  readonly startedAt: number;
  readonly endedAt: number | null;
}

/** The turns of `agent` among `spans` (in start order), oldest first. */
export function agentTurns(spans: ReadonlyMap<string, Span>, agent: string): readonly AgentTurn[] {
  const turns: AgentTurn[] = [];
  for (const span of spans.values()) {
    if (span.label?.kind !== 'agent_turn' || stringAttr(span, 'agent') !== agent) continue;
    const title = span.label.title;
    turns.push({
      span: span.id,
      number: turns.length + 1,
      headline:
        title === undefined || title === null || title === ''
          ? null
          : (declaredRoleName(title) ?? title),
      status: span.status,
      startedAt: span.started_at,
      endedAt: span.ended_at ?? null,
    });
  }
  return turns;
}

/**
 * The model an agent runs on. A turn names its model only when its role sets one, so an agent on
 * the run's default has none in the trace; the executions the run is running carry the resolved
 * model by role, so a role that runs now gives its agents' model. Null when neither names it.
 */
export function agentModel(agent: AgentNode, core: CoreState | null): string | null {
  if (agent.model !== null) return agent.model;
  const models = new Set<string>();
  for (const execution of Object.values(core?.activeExecutions ?? {})) {
    if (execution.agentKind === agent.role && execution.model) models.add(execution.model);
  }
  const [only, ...rest] = models;
  return only !== undefined && rest.length === 0 ? only : null;
}
