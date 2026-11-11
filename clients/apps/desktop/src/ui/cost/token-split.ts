/**
 * A usage's tokens as principle 19 writes them: the input apart from the output, the input with the
 * share of it that was read from a cache, and a kind nobody reported as `unknown`, never zero. The
 * host's kinds do not overlap (`total_tokens` is their sum), so the input is the sum of its three
 * kinds: fresh input, cache reads and cache writes. Tokens are counts, not costs: this sums counts
 * the host served and prices nothing. Before a turn has finished every part is `—`, not `unknown`.
 */
import type {CostUsage} from '../../state/index.js';
import {UNKNOWN} from '../plain-words.js';
import {formatTokens, NOT_REPORTED} from './words.js';

export interface TokenSplit {
  readonly input: string;
  /** The cache reads' share of the input, in whole percent. */
  readonly cached: string;
  readonly output: string;
}

export function tokenSplit(usage: CostUsage, turns: number): TokenSplit {
  // No turn has finished: there is nothing to report yet, which is not the same as nobody reporting.
  if (turns === 0) return {input: NOT_REPORTED, cached: NOT_REPORTED, output: NOT_REPORTED};
  const kinds = [usage.inputTokens, usage.cacheReadTokens, usage.cacheCreationTokens].filter(
    (tokens): tokens is number => tokens !== null,
  );
  const input = kinds.length === 0 ? null : kinds.reduce((sum, tokens) => sum + tokens, 0);
  const share =
    input === null || input === 0 || usage.cacheReadTokens === null
      ? null
      : usage.cacheReadTokens / input;
  return {
    input: input === null ? UNKNOWN : formatTokens(input),
    cached:
      usage.cacheReadTokens === null
        ? UNKNOWN
        : share === null
          ? NOT_REPORTED
          : share > 0 && share < 0.005
            ? '<1%'
            : `${Math.round(share * 100)}%`,
    output: usage.outputTokens === null ? UNKNOWN : formatTokens(usage.outputTokens),
  };
}
