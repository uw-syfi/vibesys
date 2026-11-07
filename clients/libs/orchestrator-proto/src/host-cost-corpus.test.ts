import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  decodeValidated,
  decodeValidatedJson,
  encodeMessage,
  HostCostSchema,
  OtherRunsCostSchema,
  RunStatus,
  UsageSchema,
  type WatchHostCostRequest,
  WatchHostCostRequestSchema,
  type WatchHostCostResponse,
  WatchHostCostResponseSchema,
} from './index.js';

// The frames the host's Go code works out from scripted runs (orchestrator/internal/wire), read here
// through the generated TypeScript bindings and the contract's rules.
const corpus = new URL(
  '../../../../proto/vibesys/orchestrator/v1/conformance/host_cost/',
  import.meta.url,
);

const read = (name: string): WatchHostCostResponse =>
  decodeValidatedJson(
    WatchHostCostResponseSchema,
    JSON.parse(readFileSync(new URL(name, corpus), 'utf8')),
  );

describe('host cost stream conformance', () => {
  const files = readdirSync(corpus)
    .filter(name => name.endsWith('.json'))
    .sort();
  test('the whole corpus is registered', () => {
    expect(files).toEqual(['empty.json', 'live.json', 'unpriced.json']);
  });
  for (const name of files) {
    test(`${name} meets the contract and survives the wire`, () => {
      const frame = read(name);
      expect(
        decodeValidated(
          WatchHostCostResponseSchema,
          encodeMessage(WatchHostCostResponseSchema, frame),
        ),
      ).toEqual(frame);
    });
  }

  test('runs with priced and unpriced turns are ranked, split and counted by the host', () => {
    const cost = read('live.json').cost;
    expect(cost?.total?.costUsd).toBe(3.75);
    expect(cost?.runCount).toBe(3);
    expect(cost?.unpricedTurns).toBe(1);
    // The most costly run first, and the run with no priced turn last, with no cost to show.
    expect(cost?.runs.map(run => run.runId)).toEqual(['run-b', 'run-a', 'run-c']);
    expect(cost?.runs.at(-1)?.usage?.costUsd).toBeUndefined();
    expect(cost?.runs.find(run => run.runId === 'run-a')?.maxCostUsd).toBe(500);
    expect(cost?.runs.find(run => run.runId === 'run-a')?.status).toBe(RunStatus.ENDED);
    expect(cost?.otherRuns).toBeUndefined();
    expect(cost?.series?.buckets.reduce((sum, bucket) => sum + bucket.costUsd, 0)).toBe(3.75);
  });

  test('a host with nothing priced has no cost to show, not a zero', () => {
    const cost = read('unpriced.json').cost;
    expect(cost?.total?.costUsd).toBeUndefined();
    expect(cost?.lastHourUsd).toBeUndefined();
    expect(cost?.unpricedTurns).toBe(1);
    expect(read('empty.json').cost?.runCount).toBe(0);
  });

  // The rules the Go validator holds are held by the JavaScript one too, including the arithmetic of
  // the run count over the runs listed and the runs summed.
  test('the run count must be the runs listed plus the runs summed', () => {
    const live = read('live.json').cost;
    if (live === undefined) throw new Error('the corpus has no cost');
    const others = create(OtherRunsCostSchema, {
      runs: 4,
      turns: 2,
      unpricedTurns: 1,
      usage: create(UsageSchema, {costUsd: 1}),
    });
    const bytes = (runCount: number, otherRuns?: typeof others) =>
      encodeMessage(HostCostSchema, create(HostCostSchema, {...live, runCount, otherRuns}));
    expect(() => decodeValidated(HostCostSchema, bytes(3))).not.toThrow();
    expect(() => decodeValidated(HostCostSchema, bytes(7, others))).not.toThrow();
    expect(() => decodeValidated(HostCostSchema, bytes(3, others))).toThrow(/run_count/);
    expect(() => decodeValidated(HostCostSchema, bytes(6))).toThrow(/run_count/);
    expect(() =>
      decodeValidated(
        HostCostSchema,
        encodeMessage(HostCostSchema, create(HostCostSchema, {...live, unpricedTurns: 9})),
      ),
    ).toThrow(/unpriced_turns/);
  });

  test('a window begins after the epoch', () => {
    const request = (since: WatchHostCostRequest['since']) =>
      encodeMessage(WatchHostCostRequestSchema, create(WatchHostCostRequestSchema, {since}));
    expect(() => decodeValidated(WatchHostCostRequestSchema, request(undefined))).not.toThrow();
    expect(() =>
      decodeValidated(WatchHostCostRequestSchema, request(timestampFromMs(1_700_000_000_000))),
    ).not.toThrow();
    expect(() =>
      decodeValidated(WatchHostCostRequestSchema, request(timestampFromMs(0))),
    ).toThrow();
  });
});
