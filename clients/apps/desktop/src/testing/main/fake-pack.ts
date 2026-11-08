/**
 * A gateway packer for tests that run under the virtual clock: it compresses where it is called,
 * so provisioning needs no other thread and no real time (the production packer is a gzip on the
 * thread pool, which a virtual clock does not wait for).
 */
import {gzipSync} from 'node:zlib';
import * as Effect from 'effect/Effect';

export const packInPlace = (bytes: Uint8Array): Effect.Effect<Uint8Array> =>
  Effect.sync(() => new Uint8Array(gzipSync(bytes)));
