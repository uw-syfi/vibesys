import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {
  decodeValidated,
  decodeValidatedJson,
  encodeMessage,
  WatchRunsResponseSchema,
} from './index.js';

const corpus = new URL(
  '../../../../proto/vibesys/orchestrator/v1/conformance/search/',
  import.meta.url,
);

describe('search stream conformance', () => {
  const files = readdirSync(corpus)
    .filter(name => name.endsWith('.json'))
    .sort();
  test('the whole corpus is registered', () => {
    expect(files).toEqual(['live.json', 'reconnect-snapshot.json', 'retained-unreadable.json']);
  });
  for (const name of files) {
    test(name, () => {
      const frame = decodeValidatedJson(
        WatchRunsResponseSchema,
        JSON.parse(readFileSync(new URL(name, corpus), 'utf8')),
      );
      expect(
        decodeValidated(WatchRunsResponseSchema, encodeMessage(WatchRunsResponseSchema, frame)),
      ).toEqual(frame);
    });
  }
});
