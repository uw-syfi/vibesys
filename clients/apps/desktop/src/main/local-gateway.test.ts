import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import * as TestClock from 'effect/testing/TestClock';
import hostProgram from '../../../../../proto/testdata/host_program.golden.json' with {
  type: 'json',
};
import {joinDriving, untilTrue} from '../testing/main/drive.js';
import {fakeGateway} from '../testing/main/fake-gateway.js';
import {fakeLocalHost} from '../testing/main/fake-local-host.js';
import {fakeSpawner} from '../testing/main/fake-process.js';
import {GatewayArtifacts} from './gateway-provision.js';
import {dialLocalGateway} from './local-gateway.js';

const ARTIFACT = new Uint8Array([1, 2, 3]);
const artifacts = Layer.succeed(GatewayArtifacts, {read: () => Effect.succeed(ARTIFACT)});
const BINARY = `/home/user/.vibesys/gateway/0123456789abcdef/${hostProgram.program}`;

describe('local gateway dial', () => {
  it.effect('installs the gateway, starts it once, and dials its socket when it appears', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      const peer = fakeGateway();
      const spawner = fakeSpawner(() => {
        // The started gateway needs a moment to listen; the host signals it when it does.
      });
      const scope = yield* Scope.make();
      const dial = yield* Effect.forkChild(
        dialLocalGateway().pipe(
          Effect.provide(Layer.mergeAll(host.layer, artifacts, spawner.layer)),
          Effect.provideService(Scope.Scope, scope),
        ),
      );
      yield* untilTrue(() => spawner.started.length === 1);
      host.touch();
      host.touch();
      yield* Effect.promise(() => new Promise(resolve => setImmediate(resolve)));
      expect(spawner.started).toHaveLength(1);
      host.listen(peer);
      const duplex = yield* joinDriving(dial);
      expect(duplex.destroyed).toBe(false);
      expect(host.replaced).toEqual([BINARY]);
      expect(spawner.started[0]?.command).toBe(BINARY);
      expect(spawner.started[0]?.args).toEqual(['connect']);
      // The starter is retired once the dial is done; the connection stays.
      expect(spawner.started[0]?.process.signals).toEqual(['SIGTERM']);
      yield* Scope.close(scope, Exit.void);
      expect(duplex.destroyed).toBe(true);
    }),
  );

  it.effect('keeps a matching executable and dials a socket that already listens', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      host.files.set(BINARY, ARTIFACT);
      host.listen(fakeGateway());
      const spawner = fakeSpawner(() => {});
      const scope = yield* Scope.make();
      yield* dialLocalGateway().pipe(
        Effect.provide(Layer.mergeAll(host.layer, artifacts, spawner.layer)),
        Effect.provideService(Scope.Scope, scope),
      );
      expect(host.replaced).toEqual([]);
      expect(spawner.started).toEqual([]);
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect('replaces an executable from another build', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      const oldBinary = BINARY.replace(hostProgram.program, 'vibesysd');
      const oldBytes = new Uint8Array([8, 7, 6]);
      host.files.set(oldBinary, oldBytes);
      host.files.set(BINARY, new Uint8Array([9]));
      host.listen(fakeGateway());
      const scope = yield* Scope.make();
      yield* dialLocalGateway().pipe(
        Effect.provide(Layer.mergeAll(host.layer, artifacts, fakeSpawner(() => {}).layer)),
        Effect.provideService(Scope.Scope, scope),
      );
      expect(host.replaced).toEqual([BINARY]);
      expect(host.files.get(BINARY)).toEqual(ARTIFACT);
      expect(host.files.get(oldBinary)).toEqual(oldBytes);
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect("fails with the starter's exit status when it ends before the socket appears", () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      const spawner = fakeSpawner(started => started.process.end(3));
      const failure = yield* Effect.flip(
        dialLocalGateway().pipe(
          Effect.provide(Layer.mergeAll(host.layer, artifacts, spawner.layer)),
          Effect.scoped,
        ),
      );
      expect(failure.message).toBe('gateway startup exited: 3');
    }),
  );

  it.effect('gives the gateway 15 seconds to listen', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      const spawner = fakeSpawner(() => {});
      const dial = yield* Effect.forkChild(
        Effect.flip(
          dialLocalGateway().pipe(
            Effect.provide(Layer.mergeAll(host.layer, artifacts, spawner.layer)),
            Effect.scoped,
          ),
        ),
      );
      yield* untilTrue(() => spawner.started.length === 1);
      yield* TestClock.adjust('15 seconds');
      expect((yield* joinDriving(dial)).message).toBe('gateway startup timed out');
      expect(spawner.started[0]?.process.signals).toContain('SIGTERM');
    }),
  );

  it.effect('refuses a platform it ships no gateway for', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost('win32 x64');
      const failure = yield* Effect.flip(
        dialLocalGateway().pipe(
          Effect.provide(Layer.mergeAll(host.layer, artifacts, fakeSpawner(() => {}).layer)),
          Effect.scoped,
        ),
      );
      expect(failure.message).toContain('unsupported gateway platform');
    }),
  );
});
