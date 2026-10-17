/**
 * What the run's tokens are, joined to what the Run screen shows them beside: each agent (the
 * sum over its turns) and each turn. The run fold keeps usage by agent-turn span
 * (`CoreState.tokens`); the trace says which agent a span's turn is, so the join reads both and
 * keeps no state of its own.
 */
import type {Span} from '@vibesys/contracts';
import {addTokens, NO_TOKENS, type TokenLedger, type TokenTally} from '@vibesys/run-state';
import {stringAttr} from './span-reading.js';

/**
 * The usage of each agent's turns, by agent id: the sum over the turns of `spans` the ledger
 * holds usage for. An agent none of whose turns reported usage has no entry.
 */
export function tokensByAgent(
  spans: ReadonlyMap<string, Span>,
  ledger: TokenLedger,
): ReadonlyMap<string, TokenTally> {
  const byAgent = new Map<string, TokenTally>();
  for (const [id, tally] of Object.entries(ledger.bySpan)) {
    const span = spans.get(id);
    if (span?.label?.kind !== 'agent_turn') continue;
    const agent = stringAttr(span, 'agent');
    if (agent === null) continue;
    byAgent.set(agent, addTokens(byAgent.get(agent) ?? NO_TOKENS, tally));
  }
  return byAgent;
}

/** The usage of one turn, or null when its turn reported none (it runs, or the run lost it). */
export function turnTokens(ledger: TokenLedger, span: string): TokenTally | null {
  return ledger.bySpan[span] ?? null;
}
