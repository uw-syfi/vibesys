import type {SkeletonCache} from '../cache-role.js';
import {blobValue, keyframeKey, keyframeValue, sameBytes} from './values.js';

/** One database contains immutable blobs and run-scoped keyframes; no eviction or TTL. */
export class IndexedDbSkeletonCache implements SkeletonCache {
  private constructor(private readonly database: IDBDatabase) {}

  static open(factory: IDBFactory, name = 'vibesys-run-skeleton'): Promise<IndexedDbSkeletonCache> {
    return new Promise((resolve, reject) => {
      const request = factory.open(name, 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore('blobs');
        request.result.createObjectStore('keyframes');
      };
      request.onerror = () => reject(request.error);
      let blocked = false;
      request.onblocked = () => {
        blocked = true;
        reject(new Error('cache: database upgrade blocked'));
      };
      request.onsuccess = () => {
        const database = request.result;
        if (blocked) {
          database.close();
          return;
        }
        database.onversionchange = () => database.close();
        resolve(new IndexedDbSkeletonCache(database));
      };
    });
  }

  close(): void {
    this.database.close();
  }

  getBlob(hash: string): Promise<Uint8Array | undefined> {
    return this.read('blobs', hash);
  }
  async putBlob(hash: string, data: Uint8Array): Promise<void> {
    await this.write('blobs', hash, await blobValue(hash, data));
  }
  async getKeyframe(runId: string, seq: bigint): Promise<Uint8Array | undefined> {
    return this.read('keyframes', keyframeKey(runId, seq));
  }
  async putKeyframe(runId: string, seq: bigint, data: Uint8Array): Promise<void> {
    return this.write('keyframes', keyframeKey(runId, seq), keyframeValue(seq, data));
  }

  private read(store: string, key: IDBValidKey): Promise<Uint8Array | undefined> {
    return new Promise((resolve, reject) => {
      const tx = this.database.transaction(store, 'readonly');
      const request = tx.objectStore(store).get(key);
      tx.oncomplete = () =>
        resolve(request.result === undefined ? undefined : new Uint8Array(request.result));
      tx.onabort = () => reject(tx.error ?? new Error('cache: read aborted'));
      tx.onerror = () => reject(tx.error);
    });
  }

  private write(store: string, key: IDBValidKey, bytes: Uint8Array): Promise<void> {
    return new Promise((resolve, reject) => {
      const tx = this.database.transaction(store, 'readwrite');
      const objectStore = tx.objectStore(store);
      const request = objectStore.get(key);
      let conflict = false;
      request.onsuccess = () => {
        const previous = request.result;
        if (previous !== undefined && !sameBytes(new Uint8Array(previous), bytes)) {
          conflict = true;
          tx.abort();
        } else if (previous === undefined) objectStore.add(bytes, key);
      };
      tx.oncomplete = () => resolve();
      tx.onabort = () =>
        reject(
          conflict
            ? new Error('cache: immutable key conflict')
            : (tx.error ?? new Error('cache: write aborted')),
        );
      tx.onerror = () => reject(tx.error);
    });
  }
}
