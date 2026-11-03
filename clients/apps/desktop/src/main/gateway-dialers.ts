/**
 * How each host's gateway is reached: this machine by its Unix socket, an ssh host by a channel
 * over the host's one shared ssh link that runs the gateway's stdio bridge.
 */
import {Duplex} from 'node:stream';
import * as Effect from 'effect/Effect';
import type * as Scope from 'effect/Scope';
import {type HostKey, hostFromKey} from '../state/host.js';
import {GatewayFailed, type LinkFailed} from './errors.js';
import type {GatewayDialer} from './gateway-connection.js';
import {GATEWAY_CONNECT_SCRIPT, GatewayArtifacts, provisionGateway} from './gateway-provision.js';
import type {LinkPool} from './link-pool.js';
import {dialLocalGateway} from './local-gateway.js';
import {LocalHost} from './local-host.js';
import {ProcessSpawner, type SpawnedProcess} from './process.js';
import type {SshLink} from './ssh-link.js';

/** The newest bytes of a bridge process's stderr kept for the log. */
const STDERR_TAIL = 4096;

/**
 * The gateway process's pipes as one duplex. When the process ends, the duplex is destroyed and
 * the link's words for how it ended, with the tail of what the process said, go to `log`.
 */
const bridge = (
  process: SpawnedProcess,
  link: Pick<SshLink, 'explain'>,
  log: (line: string) => void,
): Effect.Effect<Duplex, never, Scope.Scope> =>
  Effect.gen(function* () {
    const duplex = Duplex.from({readable: process.stdout, writable: process.stdin});
    // Destroying the pipes ends the duplex with an abort error that no one is left to read; the
    // session on top owns real failures.
    duplex.on('error', () => {});
    yield* Effect.addFinalizer(() => Effect.sync(() => duplex.destroy()));
    let stderr = '';
    process.stderr.setEncoding('utf8');
    process.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-STDERR_TAIL);
    });
    yield* Effect.forkScoped(
      Effect.flatMap(process.exit, code =>
        Effect.sync(() => {
          const tail = stderr.trim();
          const cause = link.explain(code, tail, 'the gateway').message;
          const said = tail === '' ? 'no output from ssh' : tail;
          log(`gateway connect exited: ${code} (${cause})\n${said}`);
          duplex.destroy();
        }),
      ),
    );
    return duplex;
  });

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
          dial: () => provide(dialLocalGateway()),
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
