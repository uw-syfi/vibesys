/**
 * Reaching the gateway on this machine: install the executable this build ships if it is not
 * already there, and run its stdio bridge, as the ssh hosts do. The bridge finds the host, starts
 * it when nobody serves, and replaces an idle host of another API, so this app knows nothing of the
 * socket and a local run and a remote one behave alike.
 */
import {join} from 'node:path';
import type {Duplex} from 'node:stream';
import {DesktopRefusal} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import type * as Scope from 'effect/Scope';
import {gatewayTarget} from '../state/gateway-target.js';
import {GatewayFailed, GatewayRefused} from './errors.js';
import {bridge} from './gateway-bridge.js';
import {GatewayArtifacts, HOST_PROGRAM} from './gateway-provision.js';
import {LocalHost} from './local-host.js';
import {type ProcessSpawner, spawnScoped} from './process.js';

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

/** How a bridge on this machine ending is put into words: no link can be at fault. */
const onThisMachine = {
  explain: (code: number | null, stderr: string, where: string) => ({
    message: stderr === '' ? `${where} exited: ${code}` : `${where} exited: ${code}: ${stderr}`,
  }),
};

/** A duplex to this machine's host, started if need be. Ends with the scope. */
export const dialLocalGateway = (
  log: (line: string) => void,
): Effect.Effect<
  Duplex,
  GatewayFailed | GatewayRefused,
  LocalHost | GatewayArtifacts | ProcessSpawner | Scope.Scope
> =>
  Effect.gen(function* () {
    const host = yield* LocalHost;
    const artifacts = yield* GatewayArtifacts;
    const target = yield* Effect.try({
      try: () => gatewayTarget(host.platform),
      catch: cause =>
        new GatewayRefused({
          refusal: DesktopRefusal.UNSUPPORTED_PLATFORM,
          message: cause instanceof Error ? cause.message : String(cause),
        }),
    });
    yield* host.ensureDirectory;
    // Read first to fail clearly when development skipped the Go build.
    const bytes = yield* artifacts.read(target);
    const binary = join(host.directory, HOST_PROGRAM);
    yield* install(binary, bytes);
    const process = yield* spawnScoped(binary, ['connect']).pipe(
      Effect.mapError(error => new GatewayFailed({message: error.message})),
    );
    return yield* bridge(process, onThisMachine, log);
  });
