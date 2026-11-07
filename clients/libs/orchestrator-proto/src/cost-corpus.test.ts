import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {
  decodeValidated,
  decodeValidatedJson,
  encodeMessage,
  type WatchRunCostResponse,
  WatchRunCostResponseSchema,
} from './index.js';

// The frames the host's Go code works out from scripted runs (orchestrator/internal/wire), read here
// through the generated TypeScript bindings and the contract's rules.
const corpus = new URL(
  '../../../../proto/vibesys/orchestrator/v1/conformance/cost/',
  import.meta.url,
);

const read = (name: string): WatchRunCostResponse =>
  decodeValidatedJson(
    WatchRunCostResponseSchema,
    JSON.parse(readFileSync(new URL(name, corpus), 'utf8')),
  );

describe('run cost stream conformance', () => {
  const files = readdirSync(corpus)
    .filter(name => name.endsWith('.json'))
    .sort();
  test('the whole corpus is registered', () => {
    expect(files).toEqual(['live.json', 'unpriced.json']);
  });
  for (const name of files) {
    test(`${name} meets the contract and survives the wire`, () => {
      const frame = read(name);
      expect(
        decodeValidated(
          WatchRunCostResponseSchema,
          encodeMessage(WatchRunCostResponseSchema, frame),
        ),
      ).toEqual(frame);
    });
  }

  test('a run with priced and unpriced turns is split and counted by the host', () => {
    const cost = read('live.json').cost;
    expect(cost?.total?.costUsd).toBe(5);
    expect(cost?.maxCostUsd).toBe(500);
    expect(cost?.turns).toBe(5);
    expect(cost?.unpricedTurns).toBe(1);
    // The type with no priced turn is listed last, with no cost and the turn marked unpriced.
    const profiler = cost?.roles.at(-1);
    expect(profiler?.usage?.costUsd).toBeUndefined();
    expect(profiler?.unpricedTurns).toBe(1);
    expect(cost?.series?.buckets.reduce((sum, bucket) => sum + bucket.costUsd, 0)).toBe(5);
  });

  test('a run with nothing priced has no cost to show, not a zero', () => {
    const cost = read('unpriced.json').cost;
    expect(cost?.total?.costUsd).toBeUndefined();
    expect(cost?.lastHourUsd).toBeUndefined();
    expect(cost?.maxCostUsd).toBeUndefined();
    expect(cost?.unpricedTurns).toBe(1);
  });
});
