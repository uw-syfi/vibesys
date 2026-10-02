import type {
  ControlTransport,
  ProtocolResponse,
  RunEvent,
  ServerMessage,
} from '@vibesys/backend-client';

const DEFAULT_REPLAY_FIXTURE_URL = '/__vibesys/fixtures/demo-run.jsonl';

type ReplayFetch = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * Reads the recorded run the page replays when it was served without a gateway.
 *
 * A failure is reported as an error carrying the HTTP status, so a fixture the
 * server cannot serve says what went wrong instead of leaving the page on an
 * empty transcript. `signal` abandons a request whose page has moved on, and
 * `fetchFixture` is the seam a test drives the failure through.
 */
export async function fetchReplay(
  url = DEFAULT_REPLAY_FIXTURE_URL,
  signal?: AbortSignal,
  fetchFixture: ReplayFetch = globalThis.fetch,
): Promise<string> {
  const response = await fetchFixture(url, signal === undefined ? undefined : {signal});
  if (!response.ok) throw new Error(`Replay fixture request failed with ${response.status}`);
  return response.text();
}

/**
 * The events of a JSONL recording, naming the line a malformed record is on: a
 * truncated or hand-edited fixture is a defect in the fixture, and the line
 * number is the only part of that a reader cannot work out from the message.
 */
function parseReplay(text: string): RunEvent[] {
  const events: RunEvent[] = [];
  for (const [index, line] of text.split('\n').entries()) {
    if (line.trim().length === 0) continue;
    try {
      events.push(JSON.parse(line) as RunEvent);
    } catch (reason) {
      throw new Error(`Replay fixture contains invalid JSON on line ${index + 1}`, {cause: reason});
    }
  }
  return events;
}

/**
 * A read-only transport over a recorded run, for the page served without a gateway: one
 * subscription replays every event, queries answer empty, and commands are refused.
 */
export function replayTransport(log: Promise<string>): ControlTransport {
  const events = log.then(parseReplay);
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
    // A recording has no control channel to redial, so the verb every frontend
    // affordance needs is answerable here without one.
    reconnect: () => undefined,
    close: async () => undefined,
  };
}
