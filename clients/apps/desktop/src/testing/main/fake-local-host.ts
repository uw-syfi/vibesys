/**
 * An in-memory `LocalHost`: files in a map, and one Unix socket path that is either not listening
 * or backed by a `FakeGateway`. Starting the gateway is the test's to script (see `listen`).
 */
import type {Duplex} from 'node:stream';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import {LocalHost, NotListening} from '../../main/local-host.js';
import type {FakeGateway} from './fake-gateway.js';

export interface FakeLocalHost {
  readonly layer: Layer.Layer<LocalHost>;
  /** Files by path. */
  readonly files: Map<string, Uint8Array>;
  /** Every path written with `replace`, in order. */
  readonly replaced: string[];
  /** Start listening on the socket with `gateway`, and signal the change that ends the wait. */
  readonly listen: (gateway: FakeGateway) => void;
  /** Signal a directory change in which nothing listens yet. */
  readonly touch: () => void;
}

export function fakeLocalHost(platform = 'linux x64'): FakeLocalHost {
  const directory = '/home/user/.vibesys/gateway/0123456789abcdef';
  const files = new Map<string, Uint8Array>();
  const replaced: string[] = [];
  const signals = new Set<Queue.Queue<void>>();
  let gateway: FakeGateway | null = null;
  const touch = (): void => {
    for (const queue of signals) Queue.offerUnsafe(queue, undefined);
  };
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
    dial: () =>
      Effect.suspend(() => {
        if (gateway === null) return Effect.fail(new NotListening());
        const duplex: Duplex = gateway.connect();
        return Effect.as(
          Effect.addFinalizer(() => Effect.sync(() => duplex.destroy())),
          duplex,
        );
      }),
    changes: () =>
      Effect.gen(function* () {
        const queue = yield* Queue.sliding<void>(1);
        signals.add(queue);
        yield* Effect.addFinalizer(() => Effect.sync(() => signals.delete(queue)));
        return queue;
      }),
  });
  return {
    layer,
    files,
    replaced,
    listen: next => {
      gateway = next;
      touch();
    },
    touch,
  };
}
