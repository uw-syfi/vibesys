/** The gateway executables the build ships, found under the app's resources by schema hash. */
import {API_SCHEMA_HASH} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import {GatewayFailed} from './errors.js';
import {nodeReadFile, type ReadFile} from './files.js';
import {GatewayArtifacts, HOST_PROGRAM} from './gateway-provision.js';

/**
 * Artifacts under `resources`, read with `read`. A missing build is an error naming the platform:
 * development that skipped the Go build gets a clear instruction, not an empty upload.
 */
export const gatewayArtifactsFrom = (
  resources: string,
  read: ReadFile,
): Layer.Layer<GatewayArtifacts> =>
  Layer.succeed(GatewayArtifacts, {
    read: target =>
      read(`${resources}/${API_SCHEMA_HASH.slice(0, 16)}/${HOST_PROGRAM}-${target}`).pipe(
        Effect.flatMap(bytes =>
          bytes === null ? Effect.fail(new Error('missing')) : Effect.succeed(bytes),
        ),
        Effect.mapError(
          cause =>
            new GatewayFailed({
              message: `the app needs its gateway resources: install Go and rebuild (${target}): ${cause.message}`,
            }),
        ),
      ),
  });

export const NodeGatewayArtifacts = (resources: string): Layer.Layer<GatewayArtifacts> =>
  gatewayArtifactsFrom(resources, nodeReadFile);
