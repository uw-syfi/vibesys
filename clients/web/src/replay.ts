import type {
  ProtocolResponse,
  RunEvent,
  ServerMessage,
  ServerTransport,
} from '@vibesys/backend-client';

export async function fetchReplay(url = '/__vibesys/fixtures/demo-run.jsonl'): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Replay fixture request failed with ${response.status}`);
  return response.text();
}

/**
 * A read-only transport over a recorded run, for the page served without a gateway: one
 * subscription replays every event, queries answer empty, and commands are refused.
 */
export function replayTransport(log: Promise<string>): ServerTransport {
  const events = log.then(text =>
    text
      .split('\n')
      .filter(line => line.trim().length > 0)
      .map(line => JSON.parse(line) as RunEvent),
  );
  const ok = (fields: Partial<ProtocolResponse> = {}): ProtocolResponse => ({
    request_id: 'replay',
    ok: true,
    ...fields,
  });
  return {
    async request(input) {
      const recorded = await events;
      if (input.type === 'query.snapshot') {
        const runId = recorded[0]?.run_id ?? 'replay';
        return ok({snapshot: {run_id: runId, sequence: 0, status: 'starting'}});
      }
      if (input.type?.startsWith('command.')) throw new Error('A replay is read-only');
      return ok();
    },
    async subscribe(after, onMessage) {
      const recorded = await events;
      const last = recorded.at(-1)?.sequence ?? 0;
      const subscribed: ServerMessage = {
        type: 'subscribed',
        request_id: 'replay',
        run_id: recorded[0]?.run_id ?? 'replay',
        latest_sequence: last,
      };
      onMessage(subscribed);
      onMessage({
        type: 'event_batch',
        events: recorded.filter(event => (event.sequence ?? 0) > after),
        through_sequence: last,
        active_executions: [],
        history_after_sequence: 0,
      });
      return {close: async () => undefined};
    },
    close: async () => undefined,
  };
}
