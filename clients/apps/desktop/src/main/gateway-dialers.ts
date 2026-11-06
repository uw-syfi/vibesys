/**
 * How each host's gateway is reached: through the gateway's stdio bridge, run on this machine or
 * in a channel over the host's one shared ssh link.
 */
import type {Duplex} from 'node:stream';
import * as Effect from 'effect/Effect';
import type * as Scope from 'effect/Scope';
import {type HostKey, hostFromKey} from '../state/host.js';
import {GatewayFailed, type LinkFailed} from './errors.js';
import {bridge} from './gateway-bridge.js';
import type {GatewayDialer} from './gateway-connection.js';
import {GATEWAY_CONNECT_SCRIPT, GatewayArtifacts, provisionGateway} from './gateway-provision.js';
import type {LinkPool} from './link-pool.js';
import {dialLocalGateway} from './local-gateway.js';
import {LocalHost} from './local-host.js';
import {ProcessSpawner} from './process.js';

/** A resolver from a host to the way its gateway is dialed. */
export type GatewayDialers = (host: HostKey) => GatewayDialer;

export const makeGatewayDialers = (
  pool: LinkPool,
  log: (host: HostKey, line: string) => void,
): Effect.Effect<GatewayDialers, never, LocalHost | GatewayArtifacts | ProcessSpawner> =>
  Effect.gen(function* () {
    // Capture the three services one by one: the whole context would also carry the scope this
    // factory runs in, which must not replace the scope a dial is given.
    const local = yield* LocalHost;
    const artifacts = yield* GatewayArtifacts;
    const spawner = yield* ProcessSpawner;
    const provide = <A, E>(
      effect: Effect.Effect<A, E, LocalHost | GatewayArtifacts | ProcessSpawner | Scope.Scope>,
    ): Effect.Effect<A, E, Scope.Scope> =>
      effect.pipe(
        Effect.provideService(LocalHost, local),
        Effect.provideService(GatewayArtifacts, artifacts),
        Effect.provideService(ProcessSpawner, spawner),
      );
    return (key: HostKey): GatewayDialer => {
      const host = hostFromKey(key);
      const say = (line: string): void => log(key, line);
      if (host.kind === 'local')
        return {
          dial: () => provide(dialLocalGateway(say)),
        };
      return {
        dial: force =>
          provide(
            Effect.gen(function* () {
              // The link is held for as long as the gateway channel is open.
              const link = yield* pool.lease(host.alias);
              yield* link.establish;
              yield* provisionGateway(link, force);
              const process = yield* link
                .channel(GATEWAY_CONNECT_SCRIPT)
                .pipe(Effect.mapError(error => new GatewayFailed({message: error.message})));
              return yield* bridge(process, link, say);
            }),
          ) satisfies Effect.Effect<Duplex, GatewayFailed | LinkFailed, Scope.Scope>,
      };
    };
  });
