/**
 * An in-memory `LocalHost`: files in a map.
 */
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import {LocalHost} from '../../main/local-host.js';

export interface FakeLocalHost {
  readonly layer: Layer.Layer<LocalHost>;
  /** Files by path. */
  readonly files: Map<string, Uint8Array>;
  /** Every path written with `replace`, in order. */
  readonly replaced: string[];
}

export function fakeLocalHost(platform = 'linux x64'): FakeLocalHost {
  const directory = '/home/user/.vibesys/gateway/0123456789abcdef';
  const files = new Map<string, Uint8Array>();
  const replaced: string[] = [];
  const layer = Layer.succeed(LocalHost, {
    platform,
    directory,
    ensureDirectory: Effect.void,
    read: path => Effect.sync(() => files.get(path) ?? null),
    replace: (path, bytes) =>
      Effect.sync(() => {
        replaced.push(path);
        files.set(path, bytes);
      }),
  });
  return {layer, files, replaced};
}
