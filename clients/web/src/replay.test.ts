import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {fetchReplay, replayTransport} from './replay.js';

test('folds a JSONL recording through one subscription', async () => {
  const log = '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n';
  const transport = replayTransport(Promise.resolve(log));
  const messages: {type?: string}[] = [];

  await transport.subscribe(
    0,
    message => messages.push(message),
    () => undefined,
  );

  assert.deepEqual(
    messages.map(message => message.type),
    ['subscribed', 'event_batch'],
  );
});

test('reports the HTTP status of a fixture the server would not serve', async () => {
  await assert.rejects(
    fetchReplay(undefined, undefined, async () => new Response('unavailable', {status: 503})),
    /Replay fixture request failed with 503/,
  );
});

test('reports the line containing malformed replay JSON', async () => {
  const log = '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n{\n';
  const transport = replayTransport(Promise.resolve(log));

  await assert.rejects(
    transport.request({type: 'query.snapshot'}),
    /Replay fixture contains invalid JSON on line 2/,
  );
});
