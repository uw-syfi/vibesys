/** The gateway executables the build ships, found under the app's resources by schema hash. */
import {API_SCHEMA_HASH, DesktopRefusal} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import {GATEWAY_TARGETS} from '../state/gateway-target.js';
import {GatewayFailed, GatewayRefused} from './errors.js';
import {nodeReadFile, type ReadFile} from './files.js';
import {GatewayArtifacts, HOST_PROGRAM} from './gateway-provision.js';

const VERSION = API_SCHEMA_HASH.slice(0, 16);

/**
 * Artifacts under `resources`, read with `read`: the host programs this build of the app ships (a
 * packaged app) or built (a development build). A build that holds none for the host's platform
 * refuses, naming the platform, the version and the platforms it does hold, so that a dev build
 * that built fewer targets is told which one is missing and is not retried into a loop.
 */
export const gatewayArtifactsFrom = (
  resources: string,
  read: ReadFile,
): Layer.Layer<GatewayArtifacts> => {
  const pathOf = (target: string): string => `${resources}/${VERSION}/${HOST_PROGRAM}-${target}`;
  const held = Effect.forEach(GATEWAY_TARGETS, target =>
    read(pathOf(target)).pipe(
      Effect.map(bytes => (bytes === null ? [] : [target])),
      Effect.orElseSucceed(() => []),
    ),
  ).pipe(Effect.map(found => found.flat()));
  return Layer.succeed(GatewayArtifacts, {
    read: target =>
      read(pathOf(target)).pipe(
        Effect.mapError(
          cause =>
            new GatewayFailed({
              message: `could not read the host program for ${target} (API ${VERSION}): ${cause.message}`,
            }),
        ),
        Effect.flatMap(bytes =>
          bytes === null
            ? Effect.flatMap(held, found =>
                Effect.fail(
                  new GatewayRefused({
                    refusal: DesktopRefusal.HOST_PROGRAM_MISSING,
                    message: `this build has no host program for ${target} (API ${VERSION}); it holds ${found.length === 0 ? 'none' : found.join(', ')}`,
                  }),
                ),
              )
            : Effect.succeed(bytes),
        ),
      ),
  });
};

export const NodeGatewayArtifacts = (resources: string): Layer.Layer<GatewayArtifacts> =>
  gatewayArtifactsFrom(resources, nodeReadFile);
