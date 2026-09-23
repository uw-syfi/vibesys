import type {RunEvent} from '@vibesys/backend-client';
import type {CoreStateStore} from './store.js';

export async function loadReplayFixture(
  store: CoreStateStore,
  url = '/__vibesys/fixtures/framework-events.jsonl',
): Promise<void> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Replay fixture request failed with ${response.status}`);
  const events = (await response.text())
    .split('\n')
    .filter(line => line.trim().length > 0)
    .map(line => JSON.parse(line) as RunEvent);
  store.append(events);
}
