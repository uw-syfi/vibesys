/**
 * An agent's turns on the Agents face: the `agent_turn` spans whose `agent` attribute names it, in
 * the order they started, each with its headline (the label's title) and when it ran. The agent
 * graph (`@vibesys/run-state/agents`) keeps only an agent's latest turn; the pane that lists every
 * turn reads the trace here, once per agent opened.
 */
import type {Span} from '@vibesys/contracts';
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
      headline: title === undefined || title === null || title === '' ? null : title,
      status: span.status,
      startedAt: span.started_at,
      endedAt: span.ended_at ?? null,
    });
  }
  return turns;
}
