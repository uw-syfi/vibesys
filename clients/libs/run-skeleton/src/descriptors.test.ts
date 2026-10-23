import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {create, equals, fromBinary, toBinary} from '@bufbuild/protobuf';
import {FileDescriptorSetSchema} from '@bufbuild/protobuf/wkt';
import {sha256} from '@noble/hashes/sha2.js';
import {bytesToHex} from '@noble/hashes/utils.js';
import {
  DescriptorSetSchema,
  EpochVersionSchema,
  KeyframeSchema,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
} from '@vibesys/proto';
import {
  DescriptorCache,
  DescriptorsUnknown,
  type RunTransport,
  refusalOf,
  startSkeletonLoader,
} from './index.js';

const FIXTURES = new URL('../../../../proto/testdata/', import.meta.url);

/** The gateway's own reply for the converted run, pinned by the Go tests. */
const golden = () =>
  fromBinary(
    SkeletonResponseSchema,
    gunzipSync(readFileSync(new URL('skeleton-wire/converted.response.pb.gz', FIXTURES))),
  );
const stored = () =>
  fromBinary(KeyframeSchema, readFileSync(new URL('conversion/expected.pb', FIXTURES)));

test('the gateway reply names its descriptors and restores to the stored keyframe', () => {
  const reply = golden();
  const wire = fromBinary(KeyframeSchema, reply.keyframe ?? new Uint8Array());
  expect(reply.descriptorSets).toHaveLength(1);
  expect(wire.epochs.every(version => version.epoch?.descriptors === undefined)).toBe(true);
  expect(wire.epochs.map(version => version.descriptorsSha256)).toEqual([
    reply.descriptorSets[0]?.sha256 ?? '',
  ]);
  expect(reply.descriptorSets[0]?.sha256).toBe(
    bytesToHex(sha256(reply.descriptorSets[0]?.data ?? new Uint8Array())),
  );
  const restored = new DescriptorCache().restore(reply);
  expect(
    equals(
      KeyframeSchema,
      fromBinary(KeyframeSchema, restored.keyframe ?? new Uint8Array()),
      stored(),
    ),
  ).toBe(true);
});

test('a reader that kept the set is sent only the name, and refuses a name nobody sent', () => {
  const cache = new DescriptorCache();
  const reply = golden();
  cache.restore(reply);
  expect(cache.known()).toEqual([reply.descriptorSets[0]?.sha256 ?? '']);
  const warm = {...reply, descriptorSets: []};
  const restored = cache.restore(warm);
  expect(
    equals(
      KeyframeSchema,
      fromBinary(KeyframeSchema, restored.keyframe ?? new Uint8Array()),
      stored(),
    ),
  ).toBe(true);
  expect(warm.descriptorSets).toHaveLength(0);
  const failure = (() => {
    try {
      new DescriptorCache().restore(warm);
    } catch (error) {
      return error;
    }
    return undefined;
  })();
  expect(failure).toBeInstanceOf(DescriptorsUnknown);
  expect(refusalOf(failure)).toBe('data-loss');
});

test('a set that does not hash to its name is refused', () => {
  const reply = golden();
  const set = reply.descriptorSets[0];
  if (!set) throw new Error('fixture');
  const tampered = create(DescriptorSetSchema, {sha256: set.sha256, data: set.data.slice(1)});
  expect(() => new DescriptorCache().restore({...reply, descriptorSets: [tampered]})).toThrow(
    'does not match its bytes',
  );
});

/** Epochs drawn from a pool of sets, named as the gateway names them. */
test('over generated epoch layouts, restore returns the stored keyframe or a typed error', () => {
  const pool = ['a', 'b', 'c'].map((name, index) =>
    toBinary(
      FileDescriptorSetSchema,
      create(FileDescriptorSetSchema, {
        file: [{name: `${name}.proto`, package: 'p'.repeat(10 ** (index + 1))}],
      }),
    ),
  );
  let state = 11;
  const next = (bound: number) => {
    state = (state * 1103515245 + 12345) % 2 ** 31;
    return state % bound;
  };
  for (let round = 0; round < 60; round += 1) {
    const stored = create(KeyframeSchema, {throughSeq: 5n, logOffset: 8n});
    const wire = create(KeyframeSchema, {throughSeq: 5n, logOffset: 8n});
    const sent = new Map<string, Uint8Array>();
    for (let seq = 1; seq <= next(6); seq += 1) {
      const data = next(4) === 0 ? undefined : pool[next(pool.length)];
      const base = {seq: BigInt(seq), epoch: {runId: 'run-1', epoch: seq}};
      if (data === undefined) {
        stored.epochs.push(create(EpochVersionSchema, base));
        wire.epochs.push(create(EpochVersionSchema, base));
        continue;
      }
      const hash = bytesToHex(sha256(data));
      sent.set(hash, data);
      stored.epochs.push(
        create(EpochVersionSchema, {
          ...base,
          epoch: {...base.epoch, descriptors: fromBinary(FileDescriptorSetSchema, data)},
        }),
      );
      wire.epochs.push(create(EpochVersionSchema, {...base, descriptorsSha256: hash}));
    }
    const reply = create(SkeletonResponseSchema, {
      keyframe: toBinary(KeyframeSchema, wire),
      descriptorSets: [...sent].map(([hash, data]) =>
        create(DescriptorSetSchema, {sha256: hash, data}),
      ),
    });
    const restored = new DescriptorCache().restore(reply);
    expect(
      equals(
        KeyframeSchema,
        fromBinary(KeyframeSchema, restored.keyframe ?? new Uint8Array()),
        stored,
      ),
    ).toBe(true);
  }
});

test('the loader lists what it holds and a second open is sent no descriptor bytes', async () => {
  const reply = golden();
  const requests: string[][] = [];
  const transport: RunTransport = {
    async unary(_method, request) {
      const known = fromBinary(SkeletonRequestSchema, request).knownDescriptorsSha256;
      requests.push(known);
      return toBinary(
        SkeletonResponseSchema,
        known.length === 0 ? reply : {...reply, descriptorSets: []},
      );
    },
    stream: () => ({[Symbol.asyncIterator]: () => ({next: () => new Promise(() => {})})}),
  };
  const cache = new DescriptorCache();
  const schedule = () => () => {};
  for (const _ of [0, 1]) {
    const actor = startSkeletonLoader(
      transport,
      schedule,
      {runId: 'run-1', timeoutMs: 1000},
      undefined,
      cache,
    );
    await new Promise<void>(resolve => {
      const subscription = actor.subscribe(snapshot => {
        if (snapshot.context.load !== undefined) {
          subscription.unsubscribe();
          resolve();
        }
      });
    });
    actor.stop();
  }
  expect(requests).toEqual([[], [reply.descriptorSets[0]?.sha256 ?? '']]);
});
