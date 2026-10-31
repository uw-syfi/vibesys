import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {
  MutateRequestSchema,
  MutateResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
  UsageEstimateSource,
  UsageOrigin,
  UsagePricesSaveResponseSchema,
  UsagePricesStatus,
  UsagePricingStatus,
  UsageRatesRefreshResponseSchema,
  UsageSpeed,
  UsageSummaryResponseSchema,
} from '@vibesys/proto';
import fc from 'fast-check';
import {registryUsage, usageSummaryEntry} from './registry-usage.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};
const LIVE = new AbortController().signal;
const FETCHED = {seconds: 1_790_000_000n, nanos: 250_000_000};
const PRICING = {status: UsagePricingStatus.FRESH, knownModels: 3, checkedOn: '2026-10-30'};
const BOOK = {revision: 4n, overrides: [], aliases: []};

const MODELS = ['model-a', 'model-b'];

/** A summary response whose one series has the given values in `buckets`. */
function responseWith(buckets: number[], values: number[]) {
  return create(UsageSummaryResponseSchema, {
    requestId: 'r',
    series: [
      {
        key: {
          origin: UsageOrigin.RUN,
          provider: 'claude',
          model: 'model-a',
          speed: UsageSpeed.FAST,
          source: UsageEstimateSource.MODEL,
          usageSource: 0,
        },
        bucket: buckets,
        cached: values.map(BigInt),
        cacheWrite: values.map(BigInt),
        uncached: values.map(BigInt),
        output: values.map(BigInt),
        reasoning: values.map(BigInt),
        turns: values,
        approximateTurns: values,
        recordedUsd: values,
        estimateUsd: values,
        inputUsd: values,
        cacheReadUsd: values,
        cacheWriteUsd: values,
        outputUsd: values,
        speedPremiumUsd: values,
        cacheSavingsUsd: values,
      },
    ],
    pricing: {...PRICING, fetchedAt: FETCHED},
    prices: BOOK,
    pricesStatus: UsagePricesStatus.OK,
    readAt: FETCHED,
  });
}

test('property: a series reaches the app column by column, with its key in the app’s words', () => {
  fc.assert(
    fc.property(fc.array(fc.tuple(fc.nat(99), fc.nat(1_000_000)), {maxLength: 30}), pairs => {
      const buckets = pairs.map(each => each[0]);
      const values = pairs.map(each => each[1]);
      const entry = usageSummaryEntry(responseWith(buckets, values));
      const series = entry.series[0];
      expect(series).toMatchObject({
        origin: 'run',
        provider: 'claude',
        model: 'model-a',
        speed: 'fast',
        source: 'model',
        buckets,
        cached: values,
        estimateUsd: values,
        turns: values,
      });
      expect(entry.pricing).toMatchObject({status: 'fresh', fetchedAt: 1_790_000_000_250});
      expect(entry.prices.revision).toBe(4);
      expect(entry.pricesStatus).toBe('ok');
    }),
  );
});

test('prices the host cannot read are told apart from no prices', () => {
  const response = create(UsageSummaryResponseSchema, {
    ...responseWith([], []),
    pricesStatus: UsagePricesStatus.UNREADABLE,
  });
  expect(usageSummaryEntry(response).pricesStatus).toBe('unreadable');
});

test('a series with a speed this app does not know fails by naming it, not by guessing', () => {
  const response = responseWith([0], [1]);
  const key = response.series[0]?.key;
  if (key === undefined) throw new Error('no key');
  key.speed = 99 as UsageSpeed;
  expect(() => usageSummaryEntry(response)).toThrow('unknown speed');
});

function answering(reply: (bytes: Uint8Array) => Uint8Array) {
  const sent: Uint8Array[] = [];
  const usage = registryUsage({
    checkout: async () => '/src/checkout',
    unary: async (_host, bytes) => {
      sent.push(bytes);
      return reply(bytes);
    },
    mutate: async (_host, bytes) => {
      sent.push(bytes);
      return reply(bytes);
    },
  });
  return {usage, sent};
}

const idOf = (bytes: Uint8Array, schema: typeof ReadRequestSchema | typeof MutateRequestSchema) => {
  const method = fromBinary(schema, bytes).method;
  if (method.case === undefined) throw new Error('missing method');
  return method.value.requestId;
};

test('a summary question names the projects, the edges and the time, and reads the answer', async () => {
  const {usage, sent} = answering(bytes =>
    toBinary(
      ReadResponseSchema,
      create(ReadResponseSchema, {
        result: {
          case: 'usageSummary',
          value: create(UsageSummaryResponseSchema, {
            ...responseWith([0], [2]),
            requestId: idOf(bytes, ReadRequestSchema),
          }),
        },
      }),
    ),
  );
  const outcome = await usage.summary(HOST, ['/p'], [0, 900_000, 1_800_000], 1_800_000, LIVE);
  expect(outcome.kind).toBe('summary');
  const method = fromBinary(ReadRequestSchema, sent[0] ?? new Uint8Array()).method;
  expect(method.case).toBe('usageSummary');
  if (method.case !== 'usageSummary') return;
  expect(method.value.projects).toEqual(['/p']);
  expect(method.value.edges.map(each => Number(each.seconds) * 1000)).toEqual([
    0, 900_000, 1_800_000,
  ]);
  expect(Number(method.value.now?.seconds)).toBe(1800);
});

test('a refresh answers how current the host’s rates are after', async () => {
  const {usage} = answering(bytes =>
    toBinary(
      ReadResponseSchema,
      create(ReadResponseSchema, {
        result: {
          case: 'usageRatesRefresh',
          value: create(UsageRatesRefreshResponseSchema, {
            requestId: idOf(bytes, ReadRequestSchema),
            pricing: {...PRICING, status: UsagePricingStatus.CACHED, refreshDue: true},
          }),
        },
      }),
    ),
  );
  expect(await usage.refreshRates(HOST, true, 1_000, LIVE)).toMatchObject({
    kind: 'pricing',
    pricing: {status: 'cached', refreshDue: true},
  });
});

const RATE = {input: 3, output: 15, cacheRead: 0.3, cacheWrite: 3.75, cacheWrite1h: null};

function savedAs(saved: boolean) {
  return answering(bytes =>
    toBinary(
      MutateResponseSchema,
      create(MutateResponseSchema, {
        result: {
          case: 'usagePricesSave',
          value: create(UsagePricesSaveResponseSchema, {
            requestId: idOf(bytes, MutateRequestSchema),
            prices: {revision: 5n, overrides: [], aliases: [{from: 'a', to: 'b'}]},
            saved,
          }),
        },
      }),
    ),
  );
}

test('property: a book is sent whole against its revision, and the host’s answer says saved or moved', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.nat(1_000),
      fc.boolean(),
      fc.subarray(MODELS),
      async (revision, saved, models) => {
        const {usage, sent} = savedAs(saved);
        const outcome = await usage.savePrices(
          HOST,
          revision,
          models.map(model => ({model, rate: RATE})),
          [],
          LIVE,
        );
        expect(outcome).toEqual({
          kind: saved ? 'saved' : 'moved',
          prices: {revision: 5, overrides: [], aliases: [{from: 'a', to: 'b'}]},
        });
        const method = fromBinary(MutateRequestSchema, sent[0] ?? new Uint8Array()).method;
        if (method.case !== 'usagePricesSave') throw new Error('wrong mutation');
        expect(Number(method.value.baseRevision)).toBe(revision);
        expect(method.value.overrides.map(each => each.model)).toEqual(models);
        expect(method.value.overrides.every(each => each.rate?.cacheWrite1h === undefined)).toBe(
          true,
        );
      },
    ),
  );
});

test('a save is refused before any message when the app was cancelled', async () => {
  const {usage, sent} = savedAs(true);
  const stop = new AbortController();
  stop.abort();
  await expect(usage.savePrices(HOST, 0, [], [], stop.signal)).rejects.toThrow('cancelled');
  expect(sent).toEqual([]);
});
