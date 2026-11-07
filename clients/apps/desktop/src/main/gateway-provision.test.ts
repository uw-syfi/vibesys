import type {PassThrough} from 'node:stream';
import {gunzipSync, gzipSync} from 'node:zlib';
import {describe, expect, it} from '@effect/vitest';
import * as Clock from 'effect/Clock';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as Layer from 'effect/Layer';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import hostProgram from '../../../../../proto/testdata/host_program.golden.json' with {
  type: 'json',
};
import {COPY_STALL_MS} from '../state/gateway-policy.js';
import {commandOf} from '../state/remote-command.js';
import {uploadSchedule} from '../state/upload-pacing.js';
import {joinDriving} from '../testing/main/drive.js';
import {fakeSpawner} from '../testing/main/fake-process.js';
import {LinkFailed} from './errors.js';
import {
  GATEWAY_CONNECT_SCRIPT,
  GATEWAY_DIRECTORY,
  GatewayArtifacts,
  HOST_PROGRAM,
  provisionGateway,
  runCommand,
} from './gateway-provision.js';
import {makeSshLink} from './ssh-link.js';

const OPTIONS = {alias: 'host-a', controlPath: '/tmp/vsd-1/%C'};

/** A host that holds `installed` (or nothing) and answers the scripts the provisioner sends. */
function fakeHost(artifact: Uint8Array, platform = 'Linux\nx86_64\n', paced = false) {
  const host = {
    installed: false,
    files: new Map<string, Uint8Array>(),
    uploads: [] as Uint8Array[],
    scripts: [] as string[],
    corrupt: false,
    /** The upload's input pipe when the test paces how fast the host takes it. */
    sink: undefined as PassThrough | undefined,
    /** Accept what the pipe holds: the host is reading again. */
    finish: undefined as ((packed: Buffer) => void) | undefined,
  };
  const spawner = fakeSpawner(started => {
    const script = commandOf(started.args.at(-1) ?? '') ?? '';
    host.scripts.push(script);
    const {process} = started;
    if (script === 'uname -s; uname -m') return void process.end(0, platform);
    if (script.includes('gzip -dc')) {
      const chunks: Buffer[] = [];
      const finish = (packed: Buffer): void => {
        host.uploads.push(packed);
        const same = Buffer.compare(gunzipSync(packed), artifact) === 0;
        host.installed = same && !host.corrupt;
        const destination = script.match(/mv -f "[^"]+" "([^"]+)"/)?.[1];
        if (destination && host.installed)
          host.files.set(destination, new Uint8Array(gunzipSync(packed)));
        process.end(0);
      };
      host.finish = finish;
      if (paced) {
        // The test decides when the host reads, so a chunk is accepted only when it does.
        host.sink = process.input;
        return;
      }
      process.input.on('data', (chunk: Buffer) => chunks.push(chunk));
      process.input.on('end', () => finish(Buffer.concat(chunks)));
      return;
    }
    process.end(0, host.installed ? 'match' : '');
  });
  return {host, spawner};
}

/** The size of what the provisioner uploads for `bytes`: its gzip. */
const gunzipSyncLength = (bytes: Uint8Array): number => gzipSync(bytes).length;

const artifactsOf = (bytes: Uint8Array) =>
  Layer.succeed(GatewayArtifacts, {read: () => Effect.succeed(bytes)});

const provision = (bytes: Uint8Array, force: boolean, fake: ReturnType<typeof fakeHost>) =>
  Effect.gen(function* () {
    const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(fake.spawner.layer));
    yield* provisionGateway(link, force).pipe(Effect.provide(artifactsOf(bytes)));
  });

describe('gateway provisioning', () => {
  it('uses the shared host program for remote connect', () => {
    expect(HOST_PROGRAM).toBe(hostProgram.program);
    expect(GATEWAY_CONNECT_SCRIPT).toBe(
      `exec "$HOME/${GATEWAY_DIRECTORY}/${hostProgram.program}" connect`,
    );
  });
  it.effect('uploads a gzip of the artifact to a host that has none, then verifies it', () =>
    Effect.gen(function* () {
      const bytes = new TextEncoder().encode('gateway-binary');
      const fake = fakeHost(bytes);
      const oldPath = `$HOME/${GATEWAY_DIRECTORY}/vibesysd`;
      const oldBytes = new Uint8Array([9, 8, 7]);
      fake.host.files.set(oldPath, oldBytes);
      const running = yield* Effect.forkChild(provision(bytes, false, fake));
      yield* joinDriving(running);
      expect(fake.host.installed).toBe(true);
      expect(fake.host.files.get(oldPath)).toEqual(oldBytes);
      expect(fake.host.files.get(`$HOME/${GATEWAY_DIRECTORY}/${hostProgram.program}`)).toEqual(
        bytes,
      );
      expect(fake.host.scripts.every(script => !script.includes('vibesysd'))).toBe(true);
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

/** A pseudo-random artifact of `length` bytes: gzip cannot shrink it, so its size sets the chunks. */
const artifactOf = (length: number): Uint8Array => {
  let state = 123456789;
  return Uint8Array.from({length}, () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return state & 255;
  });
};

/**
 * The host takes the upload `everyMs` at a time: each read accepts what the pipe holds, and a
 * `Infinity` pace never reads. Ends when the host has the whole upload.
 */
const readEvery = (fake: ReturnType<typeof fakeHost>, everyMs: number) =>
  Effect.gen(function* () {
    while (fake.host.sink === undefined) yield* Effect.sleep('10 millis');
    const input = fake.host.sink;
    const chunks: Buffer[] = [];
    let ended = false;
    input.on('end', () => {
      ended = true;
      fake.host.finish?.(Buffer.concat(chunks));
    });
    while (!ended && Number.isFinite(everyMs)) {
      yield* Effect.sleep(`${everyMs} millis`);
      const held: unknown = input.read();
      if (held !== null) chunks.push(held as Buffer);
    }
    return yield* Effect.never;
  });

const provisionPaced = (bytes: Uint8Array, everyMs: number) =>
  Effect.gen(function* () {
    const fake = fakeHost(bytes, 'Linux\nx86_64\n', true);
    yield* Effect.forkChild(readEvery(fake, everyMs));
    const started = yield* Clock.currentTimeMillis;
    const running = yield* Effect.forkChild(Effect.exit(provision(bytes, false, fake)));
    const exit = yield* joinDriving(running, '100 millis');
    return {fake, exit, tookMs: (yield* Clock.currentTimeMillis) - started};
  });

describe('the copy of the gateway is measured by progress, not by a wall deadline', () => {
  it.effect(
    'finishes a copy that takes minutes in all, because each chunk is accepted in time',
    () =>
      Effect.gen(function* () {
        const bytes = artifactOf(400_000);
        const chunks = uploadSchedule(gunzipSyncLength(bytes)).length;
        const {fake, exit, tookMs} = yield* provisionPaced(bytes, 15_000);
        expect(exit._tag).toBe('Success');
        expect(fake.host.installed).toBe(true);
        expect(fake.host.uploads).toHaveLength(1);
        // Slow enough that one wall deadline of the old size (90 s) could not have held it.
        expect(chunks).toBeGreaterThan(6);
        expect(tookMs).toBeGreaterThan(90_000);
      }),
  );

  it.effect.prop(
    'finishes however slowly the host takes each chunk, as long as it takes it within the stall bound',
    {
      kib: Schema.Int.check(Schema.isBetween({minimum: 40, maximum: 300})),
      everyMs: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: COPY_STALL_MS - 2_000})),
    },
    ({kib, everyMs}) =>
      Effect.gen(function* () {
        const bytes = artifactOf(kib * 1024);
        const {fake, exit} = yield* provisionPaced(bytes, everyMs);
        expect(exit._tag).toBe('Success');
        expect(fake.host.installed).toBe(true);
        expect(fake.host.uploads).toHaveLength(1);
      }),
    {arbitrary: {runs: 12}},
  );

  it.effect('fails a copy that stops being accepted, one stall bound after it stopped', () =>
    Effect.gen(function* () {
      const bytes = artifactOf(200_000);
      const {fake, exit, tookMs} = yield* provisionPaced(bytes, Number.POSITIVE_INFINITY);
      expect(exit._tag).toBe('Failure');
      expect(JSON.stringify(exit)).toContain('gateway upload stalled');
      expect(fake.host.installed).toBe(false);
      // One stall window after the first chunk's pacing; the driver also moves the virtual clock
      // while the artifact is compressed for real, which a few seconds more than covers.
      expect(tookMs).toBeLessThanOrEqual(COPY_STALL_MS + 5_000);
    }),
  );
});
