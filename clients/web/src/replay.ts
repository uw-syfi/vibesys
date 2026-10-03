import type {RunEvent} from '@vibesys/backend-client';
import type {CoreStateStore} from './store.js';

export const DEFAULT_REPLAY_FIXTURE_URL = '/__vibesys/fixtures/framework-events.jsonl';

type ReplayFetch = (input: string, init?: RequestInit) => Promise<Response>;

export async function loadReplayFixture(
  store: CoreStateStore,
  url = DEFAULT_REPLAY_FIXTURE_URL,
  signal?: AbortSignal,
  fetchReplay: ReplayFetch = globalThis.fetch,
): Promise<void> {
  const response = await fetchReplay(url, signal === undefined ? undefined : {signal});
  if (!response.ok) throw new Error(`Replay fixture request failed with ${response.status}`);
  const events: RunEvent[] = [];
  for (const [index, line] of (await response.text()).split('\n').entries()) {
    if (line.trim().length === 0) continue;
    try {
      events.push(JSON.parse(line) as RunEvent);
    } catch (reason) {
      throw new Error(`Replay fixture contains invalid JSON on line ${index + 1}`, {cause: reason});
    }
  }
  store.append(events);
}
