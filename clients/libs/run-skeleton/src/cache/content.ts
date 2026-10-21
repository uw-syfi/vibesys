/** Each content transaction owns its database handle, including an open that settles late. */

import type {ContentCache} from '../reader.js';
import {IndexedDbSkeletonCache} from './indexeddb.js';

export function gatewayContentCache(factory: IDBFactory): ContentCache {
  return {
    async getBlob(hash) {
      const cache = await IndexedDbSkeletonCache.open(factory);
      try {
        return await cache.getBlob(hash);
      } finally {
        cache.close();
      }
    },
    async putBlob(hash, bytes) {
      const cache = await IndexedDbSkeletonCache.open(factory);
      try {
        await cache.putBlob(hash, bytes);
      } finally {
        cache.close();
      }
    },
  };
}
