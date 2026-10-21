import {describe, expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {KeyframeSchema} from '@vibesys/proto';
import {IDBFactory} from 'fake-indexeddb';
import type {SkeletonCache} from '../index.js';
import {FakeSkeletonCache, IndexedDbSkeletonCache} from './index.js';

const bytes = new Uint8Array([1, 2, 3]);
const hash = '039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81';
const keyframe = (seq: bigint, offset = 8n) =>
  toBinary(KeyframeSchema, create(KeyframeSchema, {throughSeq: seq, logOffset: offset}));

interface CacheSession {
  cache: SkeletonCache;
  reopen(): Promise<SkeletonCache>;
}
const factories: {[name: string]: () => Promise<CacheSession>} = {
  Fake: async () => {
    const cache = new FakeSkeletonCache();
    return {cache, reopen: async () => cache.reopen()};
  },
  IndexedDB: async () => {
    const factory = new IDBFactory();
    return {
      cache: await IndexedDbSkeletonCache.open(factory),
      reopen: () => IndexedDbSkeletonCache.open(factory),
    };
  },
};

for (const [name, open] of Object.entries(factories)) {
  describe(`${name} SkeletonCache contract`, () => {
    test('missing values, byte ownership, idempotent writes and durability across reopen', async () => {
      const session = await open();
      let cache = session.cache;
      try {
        expect(await cache.getBlob(hash)).toBeUndefined();
        expect(await cache.getKeyframe('run-a', 1n)).toBeUndefined();
        const input = bytes.slice();
        const writing = cache.putBlob(hash, input);
        input.fill(0);
        await writing;
        await cache.putBlob(hash, bytes);
        const result = await cache.getBlob(hash);
        expect(result).toEqual(bytes);
        result?.fill(0);
        expect(await cache.getBlob(hash)).toEqual(bytes);
        await cache.putKeyframe('run-a', 1n, keyframe(1n));
        cache.close();
        cache = await session.reopen();
        expect(await cache.getBlob(hash)).toEqual(bytes);
        expect(await cache.getKeyframe('run-a', 1n)).toEqual(keyframe(1n));
      } finally {
        cache.close();
      }
    });

    test('keyframes are immutable and scoped by run and exact bigint sequence', async () => {
      const {cache} = await open();
      try {
        const seq = 9007199254740993n;
        await cache.putKeyframe('run-a', seq, keyframe(seq));
        await cache.putKeyframe('run-b', seq, keyframe(seq, 9n));
        await cache.putKeyframe('run-a', seq + 1n, keyframe(seq + 1n));
        await cache.putKeyframe('run-a', seq, keyframe(seq));
        expect(await cache.getKeyframe('run-a', seq)).toEqual(keyframe(seq));
        const copy = await cache.getKeyframe('run-b', seq);
        copy?.fill(0);
        expect(await cache.getKeyframe('run-b', seq)).toEqual(keyframe(seq, 9n));
        await expect(cache.putKeyframe('run-a', seq, keyframe(seq, 10n))).rejects.toThrow(
          'immutable',
        );
        expect(await cache.getKeyframe('run-a', seq)).toEqual(keyframe(seq));
      } finally {
        cache.close();
      }
    });

    test('concurrent writes serialize and conflicting writes never replace a value', async () => {
      const {cache} = await open();
      try {
        await Promise.all([cache.putBlob(hash, bytes), cache.putBlob(hash, bytes)]);
        const results = await Promise.allSettled([
          cache.putKeyframe('run-a', 1n, keyframe(1n)),
          cache.putKeyframe('run-a', 1n, keyframe(1n, 9n)),
        ]);
        expect(results.filter(result => result.status === 'fulfilled')).toHaveLength(1);
        expect(results.filter(result => result.status === 'rejected')).toHaveLength(1);
      } finally {
        cache.close();
      }
    });

    test('invalid hashes, content and keyframe identities fail before storing', async () => {
      const {cache} = await open();
      try {
        await expect(cache.putBlob('bad', bytes)).rejects.toThrow('sha256');
        await expect(cache.putBlob(hash, new Uint8Array([4]))).rejects.toThrow('sha256');
        await expect(cache.putKeyframe('run-a', 1n, keyframe(2n))).rejects.toThrow('through_seq');
        await expect(
          Promise.resolve().then(() => cache.putKeyframe('', 1n, keyframe(1n))),
        ).rejects.toThrow('run_id');
        expect(await cache.getBlob(hash)).toBeUndefined();
        expect(await cache.getKeyframe('run-a', 1n)).toBeUndefined();
      } finally {
        cache.close();
      }
    });
  });
}
