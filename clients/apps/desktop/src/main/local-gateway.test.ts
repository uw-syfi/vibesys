import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import hostProgram from '../../../../../proto/testdata/host_program.golden.json' with {
  type: 'json',
};
import {untilTrue} from '../testing/main/drive.js';
import {fakeLocalHost} from '../testing/main/fake-local-host.js';
import {fakeSpawner} from '../testing/main/fake-process.js';
import {GatewayArtifacts} from './gateway-provision.js';
import {dialLocalGateway} from './local-gateway.js';

const ARTIFACT = new Uint8Array([1, 2, 3]);
const artifacts = Layer.succeed(GatewayArtifacts, {read: () => Effect.succeed(ARTIFACT)});
const BINARY = `/home/user/.vibesys/gateway/0123456789abcdef/${hostProgram.program}`;

describe('local gateway dial', () => {
  const dial = (log: (line: string) => void = () => {}) => dialLocalGateway(log);

  it.effect('installs the gateway and runs its bridge, which owns finding the host', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      const spawner = fakeSpawner(() => {});
      const scope = yield* Scope.make();
      const duplex = yield* dial().pipe(
        Effect.provide(Layer.mergeAll(host.layer, artifacts, spawner.layer)),
        Effect.provideService(Scope.Scope, scope),
      );
      expect(duplex.destroyed).toBe(false);
      expect(host.replaced).toEqual([BINARY]);
      expect(spawner.started).toHaveLength(1);
      expect(spawner.started[0]?.command).toBe(BINARY);
      expect(spawner.started[0]?.args).toEqual(['connect']);
      yield* Scope.close(scope, Exit.void);
      expect(duplex.destroyed).toBe(true);
      expect(spawner.started[0]?.process.signals).toEqual(['SIGTERM']);
    }),
  );

  it.effect('keeps a matching executable', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      host.files.set(BINARY, ARTIFACT);
      const spawner = fakeSpawner(() => {});
      yield* dial().pipe(
        Effect.provide(Layer.mergeAll(host.layer, artifacts, spawner.layer)),
        Effect.scoped,
      );
      expect(host.replaced).toEqual([]);
      expect(spawner.started).toHaveLength(1);
    }),
  );

  it.effect('replaces an executable from another build', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      const oldBinary = BINARY.replace(hostProgram.program, 'vibesysd');
      const oldBytes = new Uint8Array([8, 7, 6]);
      host.files.set(oldBinary, oldBytes);
      host.files.set(BINARY, new Uint8Array([9]));
      yield* dial().pipe(
        Effect.provide(Layer.mergeAll(host.layer, artifacts, fakeSpawner(() => {}).layer)),
        Effect.scoped,
      );
      expect(host.replaced).toEqual([BINARY]);
      expect(host.files.get(BINARY)).toEqual(ARTIFACT);
      expect(host.files.get(oldBinary)).toEqual(oldBytes);
    }),
  );

  it.effect('says how the bridge ended, and ends the duplex with it', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost();
      const said: string[] = [];
      const spawner = fakeSpawner(started => started.process.end(3));
      const scope = yield* Scope.make();
      const duplex = yield* dial(line => said.push(line)).pipe(
        Effect.provide(Layer.mergeAll(host.layer, artifacts, spawner.layer)),
        Effect.provideService(Scope.Scope, scope),
      );
      yield* untilTrue(() => duplex.destroyed);
      expect(said.join('')).toContain('gateway connect exited: 3');
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect('refuses a platform it ships no gateway for', () =>
    Effect.gen(function* () {
      const host = fakeLocalHost('win32 x64');
      const failure = yield* Effect.flip(
        dial().pipe(
          Effect.provide(Layer.mergeAll(host.layer, artifacts, fakeSpawner(() => {}).layer)),
          Effect.scoped,
        ),
      );
      expect(failure.message).toContain('unsupported gateway platform');
    }),
  );
});
