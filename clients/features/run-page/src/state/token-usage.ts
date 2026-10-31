/**
 * What the run's tokens are, joined to what the Run screen shows them beside: each agent (the
 * sum over its turns) and each turn. The run fold keeps usage by agent-turn span
 * (`CoreState.tokens`); the trace says which agent a span's turn is, so the join reads both and
 * keeps no state of its own.
 */
import type {Span} from '@vibesys/contracts';
import {addTokens, NO_TOKENS, type TokenLedger, type TokenTally} from '@vibesys/run-state';
import {stringAttr} from './span-reading.js';
import type {TurnSpend} from './token-words.js';

/** A span that has not ended: its turn's tokens are counted when it does. */
const IN_PROGRESS: ReadonlySet<Span['status']> = new Set(['pending', 'running', 'waiting']);

/** What a row for a turn (or an agent) shows of its spend: the tally, `running`, or nothing reported. */
export function spendOf(tally: TokenTally | null | undefined, status: Span['status']): TurnSpend {
  if (tally !== null && tally !== undefined) return tally;
  return IN_PROGRESS.has(status) ? 'running' : null;
}

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

/**
 * What the run's turns had spent at the time bar's position. At the run's end (`atEnd`) that is the
 * whole ledger, which also holds the turns no span names. At an earlier position it is the usage of
 * the turns whose span, as the viewed run (`spans`) holds it, had ended by then: a turn that had not
 * begun or still ran spent nothing yet, and a later position never has less than an earlier one.
 */
export function tokensAt(
  ledger: TokenLedger,
  spans: ReadonlyMap<string, Span>,
  atEnd: boolean,
): TokenTally {
  if (atEnd) return ledger.total;
  let spent = NO_TOKENS;
  for (const [id, tally] of Object.entries(ledger.bySpan)) {
    const span = spans.get(id);
    if (span?.label?.kind === 'agent_turn' && !IN_PROGRESS.has(span.status)) {
      spent = addTokens(spent, tally);
    }
  }
  return spent;
}
