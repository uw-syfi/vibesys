import {gunzipSync} from 'node:zlib';
import {describe, expect, it} from '@effect/vitest';
import {API_SCHEMA_HASH, DesktopRefusal} from '@vibesys/proto';
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
        const refused = yield* Effect.flip(missing.read('darwin-arm64'));
        // A refusal no retry changes, naming the platform, the version and what the build holds.
        expect(refused).toMatchObject({
          _tag: 'GatewayRefused',
          refusal: DesktopRefusal.HOST_PROGRAM_MISSING,
        });
        expect(refused.message).toContain('darwin-arm64');
        expect(refused.message).toContain(API_SCHEMA_HASH.slice(0, 16));
        expect(refused.message).toContain('holds none');
        const broken = yield* read({}).pipe(
          Effect.provide(
            gatewayArtifactsFrom('/app/gateway', () => Effect.fail(new Error('EACCES'))),
          ),
        );
        expect((yield* Effect.flip(broken.read('linux-arm64'))).message).toContain('EACCES');
      }),
  );
});

describe('the packer of the upload', () => {
  it.effect('gzips any bytes so that the host gets them back', () =>
    Effect.gen(function* () {
      const artifacts = yield* read({}).pipe(
        Effect.provide(gatewayArtifactsFrom('/app/gateway', () => Effect.succeed(null))),
      );
      for (const bytes of [new Uint8Array(0), Uint8Array.of(7), new Uint8Array(100_000).fill(3)]) {
        const packed = yield* artifacts.pack(bytes);
        expect(new Uint8Array(gunzipSync(packed))).toEqual(bytes);
      }
    }),
  );
});

describe('a dev build that built fewer targets', () => {
  it.effect('names the platforms it does hold, so the missing one is plain', () =>
    Effect.gen(function* () {
      const artifacts = yield* read({}).pipe(
        Effect.provide(
          gatewayArtifactsFrom('/app/gateway', path =>
            Effect.succeed(path.endsWith('-darwin-arm64') ? Uint8Array.of(7) : null),
          ),
        ),
      );
      const refused = yield* Effect.flip(artifacts.read('linux-amd64'));
      expect(refused.message).toContain('no host program for linux-amd64');
      expect(refused.message).toContain('it holds darwin-arm64');
      expect(yield* artifacts.read('darwin-arm64')).toEqual(Uint8Array.of(7));
    }),
  );
});
