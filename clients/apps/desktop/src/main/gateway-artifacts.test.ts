import {describe, expect, it} from '@effect/vitest';
import {API_SCHEMA_HASH} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import hostProgram from '../../../../../proto/testdata/host_program.golden.json' with {
  type: 'json',
};
import {gatewayArtifactsFrom} from './gateway-artifacts.js';
import {GatewayArtifacts} from './gateway-provision.js';

const PATH = `/app/gateway/${API_SCHEMA_HASH.slice(0, 16)}/${hostProgram.program}-linux-amd64`;

const read = (_files: Record<string, Uint8Array>) =>
  Effect.gen(function* () {
    return yield* GatewayArtifacts;
  });

describe('gateway artifacts', () => {
  it.effect('reads the executable for the target under the schema hash directory', () =>
    Effect.gen(function* () {
      const artifacts = yield* read({}).pipe(
        Effect.provide(
          gatewayArtifactsFrom('/app/gateway', path =>
            Effect.succeed(path === PATH ? Uint8Array.of(7) : null),
          ),
        ),
      );
      expect(yield* artifacts.read('linux-amd64')).toEqual(Uint8Array.of(7));
    }),
  );

  it.effect(
    'names the platform when the build is missing, and the reason when it cannot be read',
    () =>
      Effect.gen(function* () {
        const missing = yield* read({}).pipe(
          Effect.provide(gatewayArtifactsFrom('/app/gateway', () => Effect.succeed(null))),
        );
        expect((yield* Effect.flip(missing.read('darwin-arm64'))).message).toContain(
          'install Go and rebuild (darwin-arm64)',
        );
        const broken = yield* read({}).pipe(
          Effect.provide(
            gatewayArtifactsFrom('/app/gateway', () => Effect.fail(new Error('EACCES'))),
          ),
        );
        expect((yield* Effect.flip(broken.read('linux-arm64'))).message).toContain('EACCES');
      }),
  );
});
