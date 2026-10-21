/** Real-process tier: build the shipping Go executable and cross the generic main bridge. */
import {execFileSync, spawn} from 'node:child_process';
import {watch} from 'node:fs';
import {
  appendFile,
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  rm,
  stat,
  symlink,
  writeFile,
} from 'node:fs/promises';
import {connect} from 'node:net';
import {arch, platform} from 'node:os';
import {join} from 'node:path';
import {Duplex} from 'node:stream';
import {fileURLToPath} from 'node:url';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {expect, test} from '@playwright/test';
import {
  fromChild,
  GATEWAY_DIRECTORY,
  GatewayCalls,
  gatewayTarget,
  provisionGateway,
  SshLink,
  TAIL_METHOD,
} from '@vibesys/connection';
import {API_SCHEMA_HASH, RecordSchema, TailRequestSchema, TailResponseSchema} from '@vibesys/proto';

const repository = fileURLToPath(new URL('../../../../', import.meta.url));
let buildDirectory: string;
let binary: string;
test.beforeAll(async () => {
  buildDirectory = await mkdtemp('/tmp/vsg-build-');
  binary = join(buildDirectory, 'vibesysd');
  execFileSync('go', ['build', '-o', binary, './cmd/vibesysd'], {
    cwd: join(repository, 'gateway'),
    env: {...process.env, CGO_ENABLED: '0'},
    timeout: 30_000,
  });
});
test.afterAll(async () => {
  await rm(buildDirectory, {recursive: true, force: true});
});

const schedule = (callback: () => void, delay: number) => {
  const timer = setTimeout(callback, delay);
  return () => clearTimeout(timer);
};

for (const mode of ['socket', 'connect pipe'] as const) {
  test(`Tail through the generic bridge over ${mode} reads and follows run.log`, async () => {
    const root = await mkdtemp('/tmp/vsg-');
    const run = join(root, 'fixture');
    const gateway = join(root, 'gateway', API_SCHEMA_HASH.slice(0, 16));
    await mkdir(gateway, {recursive: true});
    await mkdir(run);
    await mkdir(join(root, 'runs'));
    await symlink(run, join(root, 'runs', 'run-a'));
    const log = join(run, 'run.log');
    await writeFile(log, Buffer.concat([Buffer.from('VSRLOG01'), frame(1n)]));
    const ready = deferred<void>();
    const watcher = watch(gateway, (_event, filename) => {
      if (String(filename) === 'sock') ready.resolve();
    });
    const serve = spawn(binary, ['serve', '--root', root], {stdio: 'ignore'});
    const serveEnded = new Promise<void>(resolve => serve.once('close', () => resolve()));
    serve.once('error', ready.reject);
    let pipe: ReturnType<typeof spawn> | null = null;
    let pipeEnded = Promise.resolve();
    const abort = new AbortController();
    let calls: GatewayCalls | null = null;
    try {
      await ready.promise;
      watcher.close();
      calls = new GatewayCalls(
        async () => true,
        () => ({
          dial: async () => {
            if (mode === 'socket') return connect(join(gateway, 'sock'));
            pipe = spawn(binary, ['connect', '--root', root], {stdio: 'pipe'});
            pipe.stderr?.resume();
            pipeEnded = new Promise(resolve => pipe?.once('close', () => resolve()));
            const duplex = Duplex.from({readable: pipe.stdout, writable: pipe.stdin});
            duplex.once('close', () => pipe?.kill());
            return duplex;
          },
        }),
        schedule,
      );
      const initial = deferred<void>();
      const appended = deferred<void>();
      const seqs: bigint[] = [];
      const tail = calls.stream(
        'local',
        TAIL_METHOD,
        toBinary(TailRequestSchema, create(TailRequestSchema, {runId: 'run-a', fromSeq: 1n})),
        abort.signal,
        bytes => {
          for (const bytesRecord of fromBinary(TailResponseSchema, bytes).records) {
            const record = fromBinary(RecordSchema, bytesRecord);
            seqs.push(record.seq);
            if (record.seq === 1n) initial.resolve();
            if (record.seq === 2n) appended.resolve();
          }
        },
      );
      const outcome = tail.catch(error => {
        initial.reject(error);
        appended.reject(error);
      });
      // Attach both rejection handlers before external I/O can fail.
      const deliveries = Promise.all([initial.promise, appended.promise]);
      await initial.promise;
      await appendFile(log, frame(2n));
      await deliveries;
      expect(seqs).toEqual([1n, 2n]);
      abort.abort();
      await outcome;
    } finally {
      watcher.close();
      abort.abort();
      calls?.disable('local');
      if (pipe !== null) (pipe as ReturnType<typeof spawn>).kill();
      serve.kill();
      await Promise.all([serveEnded, pipeEnded]);
      await rm(root, {recursive: true, force: true});
    }
  });
}

function deferred<T>() {
  let resolve = (_value: T): void => {};
  let reject = (_error: unknown): void => {};
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return {promise, resolve, reject};
}

function frame(seq: bigint): Buffer {
  const payload = toBinary(
    RecordSchema,
    create(RecordSchema, {
      seq,
      epoch: 1,
      body: {
        case: 'legacyEvent',
        value: {json: JSON.stringify({type: 'run_started', detail: 'x'.repeat(200)})},
      },
    }),
  );
  let crc = 0xffffffff;
  for (const byte of payload) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (crc & 1 ? 0x82f63b78 : 0);
  }
  const header: number[] = [];
  let length = payload.length;
  while (length >= 128) {
    header.push((length & 127) | 128);
    length >>>= 7;
  }
  header.push(length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32LE((crc ^ 0xffffffff) >>> 0);
  return Buffer.concat([Buffer.from(header), checksum, payload]);
}

test('SSH provisioning verifies size and SHA-256, skips matching bytes and atomically repairs corruption', async () => {
  const home = await mkdtemp('/tmp/vsg-provision-');
  const link = new SshLink({
    alias: 'host-a',
    controlPath: '/tmp/vsg-control',
    askpass: '/tmp/vsg-askpass',
    scheduleTimeout: schedule,
    spawn: (_command, args, env) =>
      fromChild(
        spawn('/bin/sh', ['-c', args.at(-1) as string], {
          stdio: 'pipe',
          env: {...process.env, ...env, HOME: home},
        }),
      ),
  });
  const bytes = await readFile(binary);
  const artifact = async (target: string) => {
    expect(target).toBe(gatewayTarget(`${platform()} ${arch()}`));
    return bytes;
  };
  const destination = join(home, GATEWAY_DIRECTORY, 'vibesysd');
  const signal = new AbortController().signal;
  try {
    await provisionGateway(link, artifact, false, signal, schedule);
    const original = await stat(destination);
    expect(original.mode & 0o777).toBe(0o700);
    await provisionGateway(link, artifact, false, signal, schedule);
    expect((await stat(destination)).ino).toBe(original.ino);
    const corrupt = Buffer.from(bytes);
    corrupt[0] = (corrupt[0] ?? 0) ^ 1;
    await writeFile(destination, corrupt);
    await provisionGateway(link, artifact, false, signal, schedule);
    expect((await readFile(destination)).equals(bytes)).toBe(true);
    expect((await stat(destination)).ino).not.toBe(original.ino);
    await writeFile(destination, bytes.subarray(0, 10));
    await provisionGateway(link, artifact, false, signal, schedule);
    expect((await readFile(destination)).equals(bytes)).toBe(true);
    expect(await readdir(join(home, GATEWAY_DIRECTORY))).toEqual(['vibesysd']);
  } finally {
    await rm(home, {recursive: true, force: true});
  }
});
