/**
 * Reaching the gateway on this machine: install the executable this build ships if it is not
 * already there, dial its Unix socket, and start it (once) if nothing listens yet. The same
 * executable the ssh hosts run, so a local run and a remote one behave alike.
 */
import {join} from 'node:path';
import type {Duplex} from 'node:stream';
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Option from 'effect/Option';
import * as Queue from 'effect/Queue';
import * as Scope from 'effect/Scope';
import {gatewayTarget} from '../state/gateway-target.js';
import {GatewayFailed} from './errors.js';
import {GatewayArtifacts} from './gateway-provision.js';
import {LocalHost} from './local-host.js';
import {type ProcessSpawner, spawnScoped} from './process.js';

/** A window about as long as a slow first start of the gateway on a busy machine. */
const STARTUP_BOUND = '15 seconds';

const sameBytes = (a: Uint8Array, b: Uint8Array): boolean =>
  a.length === b.length && a.every((byte, index) => byte === b[index]);

/**
 * Put the gateway executable at `binary`, unless it already holds these bytes. A new file of the
 * same bytes is checked by the OS again on its first run, which makes a start take 0.3 s where
 * the file already run starts in 0.04 s, so a gateway that has to start again (after a login,
 * say) keeps its file. A different build replaces it by an atomic rename, which also permits
 * replacing a binary an older serving process still has open.
 */
const install = (binary: string, bytes: Uint8Array) =>
  Effect.gen(function* () {
    const host = yield* LocalHost;
    const installed = yield* host.read(binary);
    if (installed === null || !sameBytes(installed, bytes)) yield* host.replace(binary, bytes);
  });

/** A duplex to this machine's running gateway, started if need be. Ends with the scope. */
export const dialLocalGateway = (): Effect.Effect<
  Duplex,
  GatewayFailed,
  LocalHost | GatewayArtifacts | ProcessSpawner | Scope.Scope
> =>
  Effect.gen(function* () {
    const host = yield* LocalHost;
    const artifacts = yield* GatewayArtifacts;
    const target = yield* Effect.try({
      try: () => gatewayTarget(host.platform),
      catch: cause =>
        new GatewayFailed({message: cause instanceof Error ? cause.message : String(cause)}),
    });
    yield* host.ensureDirectory;
    // Read first to fail clearly when development skipped the Go build.
    const bytes = yield* artifacts.read(target);
    const binary = join(host.directory, 'vibesysd');
    const socket = join(host.directory, 'sock');
    yield* install(binary, bytes);

    const outer = yield* Effect.scope;
    const changes = yield* host.changes(host.directory);
    const startupFailed = Deferred.makeUnsafe<never, GatewayFailed>();
    const dialing = Effect.scoped(
      Effect.gen(function* () {
        let started = false;
        while (true) {
          const dialed = yield* host.dial(socket).pipe(
            Effect.provideService(Scope.Scope, outer),
            Effect.map(Option.some),
            Effect.catchTag('NotListening', () => Effect.succeed(Option.none<Duplex>())),
          );
          if (Option.isSome(dialed)) return dialed.value;
          if (!started) {
            started = true;
            const starter = yield* spawnScoped(binary, ['connect']).pipe(
              Effect.mapError(error => new GatewayFailed({message: error.message})),
            );
            starter.stdout.resume();
            starter.stderr.resume();
            yield* Effect.forkScoped(
              starter.exit.pipe(
                Effect.flatMap(code =>
                  Deferred.fail(
                    startupFailed,
                    new GatewayFailed({message: `gateway startup exited: ${code}`}),
                  ),
                ),
              ),
            );
          }
          yield* Queue.take(changes);
        }
      }),
    );
    const duplex = yield* Effect.raceFirst(dialing, Deferred.await(startupFailed)).pipe(
      Effect.timeoutOption(STARTUP_BOUND),
    );
    if (Option.isNone(duplex))
      return yield* new GatewayFailed({message: 'gateway startup timed out'});
    return duplex.value;
  });
