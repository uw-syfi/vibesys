/**
 * This machine, as the local gateway dial needs it: where the gateway lives, its files, and its
 * Unix socket. The `LocalHost` role keeps the filesystem and sockets out of the dial logic, so
 * that logic runs against an in-memory host; `NodeLocalHost` is the real one.
 */
import {randomUUID} from 'node:crypto';
import {watch} from 'node:fs';
import {chmod, mkdir, readFile, rename, rm, writeFile} from 'node:fs/promises';
import {connect} from 'node:net';
import {arch, homedir, platform} from 'node:os';
import {dirname, join} from 'node:path';
import type {Duplex} from 'node:stream';
import * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import type * as Scope from 'effect/Scope';
import {GatewayFailed} from './errors.js';
import {GATEWAY_DIRECTORY} from './gateway-provision.js';

/** Nothing is listening on the socket yet (no file, or no process behind it). */
export class NotListening {
  readonly _tag = 'NotListening';
}

export class LocalHost extends Context.Service<
  LocalHost,
  {
    /** `<os> <arch>` as Node names them, e.g. `darwin arm64`. */
    readonly platform: string;
    /** The directory holding the gateway executable and its socket. */
    readonly directory: string;
    readonly ensureDirectory: Effect.Effect<void, GatewayFailed>;
    /** The bytes at `path`, or null when there is no such file. */
    readonly read: (path: string) => Effect.Effect<Uint8Array | null, GatewayFailed>;
    /** Put `bytes` at `path` as an executable, replacing what is there in one atomic step. */
    readonly replace: (path: string, bytes: Uint8Array) => Effect.Effect<void, GatewayFailed>;
    /** A duplex to the socket at `path`, closed with the scope. */
    readonly dial: (
      path: string,
    ) => Effect.Effect<Duplex, NotListening | GatewayFailed, Scope.Scope>;
    /** A signal for each change in `directory`; at most one is kept while no one takes it. */
    readonly changes: (
      directory: string,
    ) => Effect.Effect<Queue.Dequeue<void>, GatewayFailed, Scope.Scope>;
  }
>()('app/LocalHost') {}

const failed = (what: string, cause: unknown): GatewayFailed =>
  new GatewayFailed({
    message: `${what}: ${cause instanceof Error ? cause.message : String(cause)}`,
  });

const NodeLocalHostImpl = (): Context.Service.Shape<typeof LocalHost> => {
  const directory = join(homedir(), GATEWAY_DIRECTORY);
  return {
    platform: `${platform()} ${arch()}`,
    directory,
    ensureDirectory: Effect.tryPromise({
      try: () => mkdir(directory, {recursive: true, mode: 0o700}),
      catch: cause => failed(`could not create ${directory}`, cause),
    }).pipe(Effect.asVoid),
    read: path =>
      Effect.tryPromise({
        try: async () => {
          try {
            return await readFile(path);
          } catch (error) {
            if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return null;
            throw error;
          }
        },
        catch: cause => failed(`could not read ${path}`, cause),
      }),
    replace: (path, bytes) =>
      Effect.tryPromise({
        try: async () => {
          const temporary = join(dirname(path), `.vibesysd-${randomUUID()}`);
          try {
            await writeFile(temporary, bytes, {flag: 'wx', mode: 0o700});
            await chmod(temporary, 0o700);
            await rename(temporary, path);
          } finally {
            await rm(temporary, {force: true});
          }
        },
        catch: cause => failed(`could not install ${path}`, cause),
      }),
    dial: path =>
      Effect.acquireRelease(
        Effect.callback<Duplex, NotListening | GatewayFailed>(resume => {
          const socket = connect(path);
          socket.once('connect', () => {
            // The session on top owns later failures; an unowned one must not crash the process.
            socket.on('error', () => {});
            resume(Effect.succeed(socket));
          });
          socket.once('error', (error: NodeJS.ErrnoException) => {
            socket.destroy();
            resume(
              Effect.fail(
                error.code === 'ENOENT' || error.code === 'ECONNREFUSED'
                  ? new NotListening()
                  : failed(`could not connect to ${path}`, error),
              ),
            );
          });
          return Effect.sync(() => socket.destroy());
        }),
        socket => Effect.sync(() => socket.destroy()),
      ),
    changes: path =>
      Effect.gen(function* () {
        const queue = yield* Queue.sliding<void>(1);
        yield* Effect.acquireRelease(
          Effect.try({
            try: () => watch(path, () => Queue.offerUnsafe(queue, undefined)),
            catch: cause => failed(`could not watch ${path}`, cause),
          }),
          watcher => Effect.sync(() => watcher.close()),
        );
        return queue;
      }),
  };
};

/** The real machine. */
export const NodeLocalHost: Layer.Layer<LocalHost> = Layer.sync(LocalHost, NodeLocalHostImpl);
