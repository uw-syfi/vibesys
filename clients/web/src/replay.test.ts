import {expect, test} from 'bun:test';
import {loadReplayFixture} from './replay.js';
import {createCoreStateStore} from './store.js';

test('loads JSONL events into the shared store', async () => {
  const store = createCoreStateStore();

  await loadReplayFixture(
    store,
    undefined,
    undefined,
    async () =>
      new Response('{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n'),
  );

  expect(store.getState().sequence).toBe(1);
});

test('reports the HTTP status without mutating the store', async () => {
  const store = createCoreStateStore();

  await expect(
    loadReplayFixture(
      store,
      undefined,
      undefined,
      async () => new Response('unavailable', {status: 503}),
    ),
  ).rejects.toThrow('Replay fixture request failed with 503');
  expect(store.getState().sequence).toBe(0);
});

test('reports the line containing malformed replay JSON', async () => {
  const store = createCoreStateStore();

  await expect(
    loadReplayFixture(
      store,
      undefined,
      undefined,
      async () =>
        new Response(
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n{\n',
        ),
    ),
  ).rejects.toThrow('Replay fixture contains invalid JSON on line 2');
  expect(store.getState().sequence).toBe(0);
});
