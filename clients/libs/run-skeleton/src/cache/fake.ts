import type {SkeletonCache} from '../cache-role.js';
import {blobValue, keyframeKey, keyframeValue, sameBytes} from './values.js';

/** Faithful in-memory cache. Reopening over the same data models durable storage. */
export class FakeSkeletonCache implements SkeletonCache {
  #closed = false;
  constructor(private readonly data = new Map<string, Uint8Array>()) {}

  reopen(): FakeSkeletonCache {
    return new FakeSkeletonCache(this.data);
  }
  close(): void {
    this.#closed = true;
  }

  async getBlob(hash: string): Promise<Uint8Array | undefined> {
    return this.get(`blob:${hash}`);
  }
  async putBlob(hash: string, data: Uint8Array): Promise<void> {
    this.assertOpen();
    const bytes = await blobValue(hash, data);
    this.put(`blob:${hash}`, bytes);
  }
  async getKeyframe(runId: string, seq: bigint): Promise<Uint8Array | undefined> {
    return this.get(JSON.stringify(keyframeKey(runId, seq)));
  }
  async putKeyframe(runId: string, seq: bigint, data: Uint8Array): Promise<void> {
    this.put(JSON.stringify(keyframeKey(runId, seq)), keyframeValue(seq, data));
  }
  private assertOpen(): void {
    if (this.#closed) throw new Error('cache: closed');
  }
  private get(key: string): Uint8Array | undefined {
    this.assertOpen();
    return this.data.get(key)?.slice();
  }
  private put(key: string, bytes: Uint8Array): void {
    this.assertOpen();
    const previous = this.data.get(key);
    if (previous && !sameBytes(previous, bytes)) throw new Error('cache: immutable key conflict');
    this.data.set(key, bytes.slice());
  }
}
