import {gunzipSync} from 'node:zlib';
import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as Layer from 'effect/Layer';
import * as TestClock from 'effect/testing/TestClock';
import {commandOf} from '../state/remote-command.js';
import {joinDriving} from '../testing/main/drive.js';
import {fakeSpawner} from '../testing/main/fake-process.js';
import {LinkFailed} from './errors.js';
import {GatewayArtifacts, provisionGateway, runCommand} from './gateway-provision.js';
import {makeSshLink} from './ssh-link.js';

const OPTIONS = {alias: 'host-a', controlPath: '/tmp/vsd-1/%C'};

/** A host that holds `installed` (or nothing) and answers the scripts the provisioner sends. */
function fakeHost(artifact: Uint8Array, platform = 'Linux\nx86_64\n') {
  const host = {
    installed: false,
    uploads: [] as Uint8Array[],
    scripts: [] as string[],
    corrupt: false,
  };
  const spawner = fakeSpawner(started => {
    const script = commandOf(started.args.at(-1) ?? '') ?? '';
    host.scripts.push(script);
    const {process} = started;
    if (script === 'uname -s; uname -m') return void process.end(0, platform);
    if (script.includes('gzip -dc')) {
      const chunks: Buffer[] = [];
      process.input.on('data', (chunk: Buffer) => chunks.push(chunk));
      process.input.on('end', () => {
        const packed = Buffer.concat(chunks);
        host.uploads.push(packed);
        const same = Buffer.compare(gunzipSync(packed), artifact) === 0;
        host.installed = same && !host.corrupt;
        process.end(0);
      });
      return;
    }
    process.end(0, host.installed ? 'match' : '');
  });
  return {host, spawner};
}

const artifactsOf = (bytes: Uint8Array) =>
  Layer.succeed(GatewayArtifacts, {read: () => Effect.succeed(bytes)});

const provision = (bytes: Uint8Array, force: boolean, fake: ReturnType<typeof fakeHost>) =>
  Effect.gen(function* () {
    const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(fake.spawner.layer));
    yield* provisionGateway(link, force).pipe(Effect.provide(artifactsOf(bytes)));
  });

describe('gateway provisioning', () => {
  it.effect('uploads a gzip of the artifact to a host that has none, then verifies it', () =>
    Effect.gen(function* () {
      const bytes = new TextEncoder().encode('gateway-binary');
      const fake = fakeHost(bytes);
      const running = yield* Effect.forkChild(provision(bytes, false, fake));
      yield* joinDriving(running);
      expect(fake.host.installed).toBe(true);
      expect(fake.host.uploads).toHaveLength(1);
      expect(fake.host.scripts.at(-1)).toContain('printf match');
    }),
  );

  it.effect('leaves a host that holds the matching gateway alone', () =>
    Effect.gen(function* () {
      const bytes = new TextEncoder().encode('gateway-binary');
      const fake = fakeHost(bytes);
      fake.host.installed = true;
      yield* provision(bytes, false, fake);
      expect(fake.host.uploads).toHaveLength(0);
    }),
  );

  it.effect('replaces the gateway when asked to, matching or not', () =>
    Effect.gen(function* () {
      const bytes = new TextEncoder().encode('gateway-binary');
      const fake = fakeHost(bytes);
      fake.host.installed = true;
      const running = yield* Effect.forkChild(provision(bytes, true, fake));
      yield* joinDriving(running);
      expect(fake.host.uploads).toHaveLength(1);
    }),
  );

  it.effect('fails when the host does not hold the artifact after the upload', () =>
    Effect.gen(function* () {
      const bytes = new TextEncoder().encode('gateway-binary');
      const fake = fakeHost(bytes);
      fake.host.corrupt = true;
      const running = yield* Effect.forkChild(Effect.flip(provision(bytes, false, fake)));
      const failure = yield* joinDriving(running);
      expect(failure.message).toContain('verification failed for linux-amd64');
    }),
  );

  it.effect('refuses a platform it ships no gateway for', () =>
    Effect.gen(function* () {
      const bytes = new Uint8Array([1]);
      const fake = fakeHost(bytes, 'FreeBSD\namd64\n');
      const failure = yield* Effect.flip(provision(bytes, false, fake));
      expect(failure.message).toContain('unsupported gateway platform');
      expect(fake.host.uploads).toHaveLength(0);
    }),
  );

  it.effect('fails a command that outlasts 30 seconds', () =>
    Effect.gen(function* () {
      const spawner = fakeSpawner(() => {});
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(spawner.layer));
      const running = yield* Effect.forkChild(Effect.flip(runCommand(link, 'sleep 99')));
      yield* Effect.yieldNow;
      yield* TestClock.adjust('30 seconds');
      expect((yield* Fiber.join(running)).message).toBe('gateway command timed out');
    }),
  );

  it.effect('words a refused channel by the link', () =>
    Effect.gen(function* () {
      const spawner = fakeSpawner(started =>
        started.process.end(255, '', 'Permission denied (publickey).'),
      );
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(spawner.layer));
      const failure = yield* Effect.flip(runCommand(link, 'true'));
      expect(failure).toBeInstanceOf(LinkFailed);
      expect(failure).toMatchObject({problem: 'auth'});
    }),
  );
});
