/**
 * Token spend folded from `turn_usage_recorded`: the run's total and each agent turn's own, the
 * turn named by the event's `span_id` (the agent-turn span of the run trace). The event is one
 * finished turn's increment, never a running total, so folding is summing; it is not the context
 * window's fill (`usage_update`, `CoreState.usage`).
 *
 * A count the provider did not report is `null`, not zero: a sum is `null` until some turn
 * reports the count, and then adds only the turns that did, so a reader can tell "none" from
 * "zero" and a sum over turns with a missing count is a lower bound. `input` includes the cached
 * parts and `output` the reasoning part, as the event does, so `totalTokens` is input plus output.
 */
import type {RunEvent} from '@vibesys/contracts';

type TurnUsageData = Extract<NonNullable<RunEvent['data']>, {kind: 'turn_usage_recorded'}>;

/** Counts of tokens spent; `null`: not reported. */
export interface TokenTally {
  /** Input tokens, cached ones included. */
  readonly input: number | null;
  /** The part of `input` the provider served from its cache. */
  readonly cacheRead: number | null;
  /** The part of `input` written to the provider's cache. */
  readonly cacheWrite: number | null;
  /** Output tokens, reasoning included. */
  readonly output: number | null;
  /** The part of `output` the provider reports as reasoning. */
  readonly reasoning: number | null;
}

/** The tally of no turn. */
export const NO_TOKENS: TokenTally = {
  input: null,
  cacheRead: null,
  cacheWrite: null,
  output: null,
  reasoning: null,
};

function plus(a: number | null, b: number | null): number | null {
  if (a === null) return b;
  return b === null ? a : a + b;
}

/** `a` and `b` summed count by count. */
export function addTokens(a: TokenTally, b: TokenTally): TokenTally {
  return {
    input: plus(a.input, b.input),
    cacheRead: plus(a.cacheRead, b.cacheRead),
    cacheWrite: plus(a.cacheWrite, b.cacheWrite),
    output: plus(a.output, b.output),
    reasoning: plus(a.reasoning, b.reasoning),
  };
}

/** What one `turn_usage_recorded` event reports. */
function tokensOf(data: TurnUsageData): TokenTally {
  return {
    input: data.input_tokens ?? null,
    cacheRead: data.cache_read_input_tokens ?? null,
    cacheWrite: data.cache_creation_input_tokens ?? null,
    output: data.output_tokens ?? null,
    reasoning: data.reasoning_output_tokens ?? null,
  };
}

/** Input plus output tokens; `null` when neither was reported. */
export function totalTokens(tally: TokenTally): number | null {
  return plus(tally.input, tally.output);
}

/** The run's spend: all turns, and each turn that the trace names. */
export interface TokenLedger {
  /** Every turn's usage, those the trace names and those it does not. */
  readonly total: TokenTally;
  /** The usage of each agent-turn span, by span id. */
  readonly bySpan: Readonly<Record<string, TokenTally>>;
}

/** The ledger before any turn finished. */
export const EMPTY_LEDGER: TokenLedger = {total: NO_TOKENS, bySpan: {}};

/** `ledger` with one finished turn's usage added to the run and to its span. */
export function ledgerWith(ledger: TokenLedger, data: TurnUsageData): TokenLedger {
  const tally = tokensOf(data);
  const span = data.span_id ?? null;
  return {
    total: addTokens(ledger.total, tally),
    bySpan:
      span === null
        ? ledger.bySpan
        : {...ledger.bySpan, [span]: addTokens(ledger.bySpan[span] ?? NO_TOKENS, tally)},
  };
}
