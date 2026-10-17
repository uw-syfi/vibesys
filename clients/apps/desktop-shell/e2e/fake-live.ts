import {RUN_ID} from './fake-trace.js';

const START = Date.UTC(2026, 9, 15, 16, 30, 51);

/**
 * More of one agent turn, as a live run sends it: `count` entries, alternating thinking and text, each
 * stamped with the turn's span id. `after` is the sequence of the last event sent so far; `from`
 * numbers the entries so each is distinct text.
 */
export function moreTurnEvents(
  span: string,
  after: number,
  from: number,
  count: number,
): readonly Record<string, unknown>[] {
  const stamp = new Date(START + 1710 * 1000).toISOString().slice(0, 19);
  return Array.from({length: count}, (_, index) => {
    const number = from + index;
    const thinking = number % 2 === 0;
    return {
      protocol_version: 1,
      run_id: RUN_ID,
      sequence: after + index + 1,
      timestamp: `${stamp}+00:00`,
      type: 'agent_output_chunk',
      invocation_id: `inv-${span}`,
      agent_kind: 'implementer',
      span_id: span,
      data: {
        kind: 'agent_output_chunk',
        channel: thinking ? 'analysis' : 'assistant',
        content: thinking
          ? `**Thought title ${number}**\n\n${`Thought number ${number}. `.repeat(8)}`
          : `Said number ${number}. `.repeat(8),
      },
    };
  });
}

/** A finished turn's structured reply, as the server sends it after the turn's streamed text. */
export function turnReplyEvent(span: string, sequence: number): Record<string, unknown> {
  return {
    protocol_version: 1,
    run_id: RUN_ID,
    sequence,
    timestamp: '2026-10-15T16:59:00+00:00',
    type: 'agent_execution_finished',
    status: 'completed',
    invocation_id: `inv-${span}`,
    agent_kind: 'implementer',
    span_id: span,
    data: {
      kind: 'agent_execution_finished',
      result: '{}',
      reply: {
        kind: 'ImplementerReply',
        json_schema: {
          properties: {
            summary: {type: 'string'},
            changed: {type: 'array', items: {type: 'string'}},
            outcome: {enum: ['done', 'blocked'], type: 'string'},
          },
          required: ['summary'],
          type: 'object',
        },
        value: {
          summary: `${'Padded both counters to their own cache line and re-measured. '.repeat(20)}`,
          changed: ['src/lib.rs'],
          outcome: 'done',
        },
      },
    },
  };
}

/**
 * Runs in the page after `installFakeServer`: `window.fakeHost.emit(events)` streams more run
 * events as one `event_batch`, a live update. Self-contained, as the page installs it by source.
 */
export function installFakeLive(): void {
  const host = (window as unknown as {fakeHost: Record<string, unknown>}).fakeHost;
  host['emit'] = (events: readonly {sequence: number}[]): void => {
    const last = events.at(-1)?.sequence;
    if (last === undefined) return;
    (host['send'] as (message: unknown) => void)({
      type: 'event_batch',
      events,
      through_sequence: last,
      store_id: 'store-1',
      history_after_sequence: 0,
    });
  };
}
