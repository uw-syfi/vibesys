import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import {hostKey} from '../state/host.js';
import {commandOf} from '../state/remote-command.js';
import {untilTrue} from '../testing/main/drive.js';
import {fakeLocalHost} from '../testing/main/fake-local-host.js';
import {fakeSpawner, type Started} from '../testing/main/fake-process.js';
import {makeGatewayDialers} from './gateway-dialers.js';
import {GATEWAY_CONNECT_SCRIPT, GatewayArtifacts} from './gateway-provision.js';
import {makeLinkPool} from './link-pool.js';

const ARTIFACT = new TextEncoder().encode('gateway');
const artifacts = Layer.succeed(GatewayArtifacts, {read: () => Effect.succeed(ARTIFACT)});
const scriptOf = (started: Started): string | null => commandOf(started.args.at(-1) ?? '');

/** An ssh host that is up and already holds the gateway; `connect` is the bridge to it. */
const upHost = () =>
  fakeSpawner(started => {
    const script = scriptOf(started);
    if (started.args.includes('check')) started.process.end(0);
    else if (script === 'uname -s; uname -m') started.process.end(0, 'Linux\nx86_64\n');
    else if (script?.includes('printf match')) started.process.end(0, 'match');
    else if (started.args.includes('exit')) started.process.end(0);
  });

describe('gateway dialers', () => {
  it.effect('reaches an ssh host over a channel on its link, after provisioning', () =>
    Effect.gen(function* () {
      const spawner = upHost();
      const lines: string[] = [];
      const poolScope = yield* Scope.make();
      const dialers = yield* Effect.gen(function* () {
        const pool = yield* makeLinkPool('/tmp/vsd-1/%C', () => {});
        return yield* makeGatewayDialers(pool, (_host, line) => lines.push(line));
      }).pipe(
        Effect.provide(Layer.mergeAll(spawner.layer, fakeLocalHost().layer, artifacts)),
        Effect.provideService(Scope.Scope, poolScope),
      );
      const scope = yield* Scope.make();
      const duplex = yield* dialers(hostKey({kind: 'ssh', alias: 'host-a'}))
        .dial(false)
        .pipe(Effect.provideService(Scope.Scope, scope));
      const bridge = spawner.started.at(-1);
      expect(scriptOf(bridge as Started)).toBe(GATEWAY_CONNECT_SCRIPT);
      // Bytes flow both ways, and the channel ends with the scope.
      const heard: Buffer[] = [];
      bridge?.process.input.on('data', (chunk: Buffer) => heard.push(chunk));
      duplex.write('hello');
      yield* untilTrue(() => heard.length > 0);
      expect(Buffer.concat(heard).toString()).toBe('hello');
      const received: Buffer[] = [];
      duplex.on('data', (chunk: Buffer) => received.push(chunk));
      bridge?.process.stdout.write('world');
      yield* untilTrue(() => received.length > 0);
      expect(Buffer.concat(received).toString()).toBe('world');
      yield* Scope.close(scope, Exit.void);
      expect(bridge?.process.signals).toEqual(['SIGTERM']);
      expect(duplex.destroyed).toBe(true);
      // The link outlives the dial only as long as someone holds it: the pool releases it.
      yield* Scope.close(poolScope, Exit.void);
      expect(spawner.started.some(s => s.args.includes('exit'))).toBe(true);
    }),
  );

  it.effect("logs how the channel ended, in the link's words, and closes the duplex", () =>
    Effect.gen(function* () {
      const spawner = upHost();
      const lines: string[] = [];
      const poolScope = yield* Scope.make();
      const dialers = yield* Effect.gen(function* () {
        const pool = yield* makeLinkPool('/tmp/vsd-1/%C', () => {});
        return yield* makeGatewayDialers(pool, (_host, line) => lines.push(line));
      }).pipe(
        Effect.provide(Layer.mergeAll(spawner.layer, fakeLocalHost().layer, artifacts)),
        Effect.provideService(Scope.Scope, poolScope),
      );
      const scope = yield* Scope.make();
      const duplex = yield* dialers(hostKey({kind: 'ssh', alias: 'host-a'}))
        .dial(false)
        .pipe(Effect.provideService(Scope.Scope, scope));
      duplex.on('error', () => {});
      spawner.started.at(-1)?.process.end(255, '', 'Permission denied (publickey).');
      yield* untilTrue(() => duplex.destroyed && lines.length > 0);
      expect(lines[0]).toContain('gateway connect exited: 255');
      expect(lines[0]).toContain('Permission denied (publickey).');
      yield* Scope.close(scope, Exit.void);
      yield* Scope.close(poolScope, Exit.void);
    }),
  );

  it.effect('reaches this machine by its own bridge, over no link', () =>
    Effect.gen(function* () {
      const local = fakeLocalHost();
      const spawner = fakeSpawner(() => {});
      const poolScope = yield* Scope.make();
      const dialers = yield* Effect.gen(function* () {
        const pool = yield* makeLinkPool('/tmp/vsd-1/%C', () => {});
        return yield* makeGatewayDialers(pool, () => {});
      }).pipe(
        Effect.provide(Layer.mergeAll(spawner.layer, local.layer, artifacts)),
        Effect.provideService(Scope.Scope, poolScope),
      );
      const scope = yield* Scope.make();
      const duplex = yield* dialers(hostKey({kind: 'local'}))
        .dial(false)
        .pipe(Effect.provideService(Scope.Scope, scope));
      expect(duplex.destroyed).toBe(false);
      expect(spawner.started.map(started => started.args)).toEqual([['connect']]);
      expect(spawner.started.map(started => started.command)).not.toContain('ssh');
      yield* Scope.close(scope, Exit.void);
      yield* Scope.close(poolScope, Exit.void);
    }),
  );
});
