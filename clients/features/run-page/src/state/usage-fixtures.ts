/** The run's token ledger as the fold builds it, from the turns' readings: plain mocked run data. */
import type {RunEvent} from '@vibesys/contracts';
import {initialCoreState, reduceEvent, type TokenLedger} from '@vibesys/run-state';

/** What one finished turn reported; a count left out is one the provider did not report. */
export interface TurnReading {
  readonly span: string;
  readonly input?: number;
  readonly cached?: number;
  readonly output?: number;
  readonly reasoning?: number;
}

/** The ledger after `turns` finished, in order, each as a `turn_usage_recorded` event. */
export function ledgerOf(turns: readonly TurnReading[]): TokenLedger {
  return turns.reduce(
    (state, turn, index) =>
      reduceEvent(state, {
        protocol_version: 1,
        run_id: 'run',
        sequence: index + 1,
        timestamp: '2026-01-01T00:00:00Z',
        type: 'turn_usage_recorded',
        data: {
          kind: 'turn_usage_recorded',
          span_id: turn.span,
          ...(turn.input === undefined ? {} : {input_tokens: turn.input}),
          ...(turn.cached === undefined ? {} : {cache_read_input_tokens: turn.cached}),
          ...(turn.output === undefined ? {} : {output_tokens: turn.output}),
          ...(turn.reasoning === undefined ? {} : {reasoning_output_tokens: turn.reasoning}),
        },
      } as RunEvent),
    initialCoreState(),
  ).tokens;
}
