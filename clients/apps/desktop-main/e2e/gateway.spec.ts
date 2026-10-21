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
import {arch, platform} from 'node:os';
import {join} from 'node:path';
import {Transform} from 'node:stream';
import {fileURLToPath} from 'node:url';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  type ElectronApplication,
  _electron as electron,
  expect,
  type Page,
  test,
} from '@playwright/test';
import {
  fromChild,
  GATEWAY_DIRECTORY,
  gatewayTarget,
  provisionGateway,
  SshLink,
  TAIL_METHOD,
} from '@vibesys/connection';
import {
  API_SCHEMA_HASH,
  HelloRequestSchema,
  HelloResponseSchema,
  RecordSchema,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {electronRuntimeArgs} from './electron-launch-policy.mjs';

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
    let app: ElectronApplication | null = null;
    try {
      await ready.promise;
      watcher.close();
      app = await electron.launch({
        args: [
          fileURLToPath(new URL('./gateway-app.mjs', import.meta.url)),
          ...electronRuntimeArgs(),
        ],
        env: {
          ...process.env,
          VIBESYS_GATEWAY_ROOT: root,
          VIBESYS_GATEWAY_BINARY: binary,
          VIBESYS_GATEWAY_MODE: mode,
        },
      });
      const page = await app.firstWindow();
      await app.evaluate(async () => {
        await (globalThis as typeof globalThis & {vibesysGatewayReady: Promise<void>})
          .vibesysGatewayReady;
      });
      // Main-process readiness and Playwright's renderer debugger use separate connections.
      // Observe the target document's load in Playwright before evaluating in its context.
      await page.waitForURL('app://vibesys/gateway.html', {waitUntil: 'load'});
      const hello = await page.evaluate(
        async request => {
          const bridge = (window as unknown as GatewayRenderer).vibesysGateway;
          return [
            ...(await bridge.unary(
              'local',
              '/vibesys.api.v1.HostService/Hello',
              new Uint8Array(request),
            ).result),
          ];
        },
        [
          ...toBinary(
            HelloRequestSchema,
            create(HelloRequestSchema, {apiSchemaHash: API_SCHEMA_HASH}),
          ),
        ],
      );
      expect(fromBinary(HelloResponseSchema, new Uint8Array(hello)).apiSchemaHash).toBe(
        API_SCHEMA_HASH,
      );
      await startTail(page);
      const initial = await page.evaluate(() =>
        (window as unknown as GatewayRenderer).gatewayRead(),
      );
      expect(sequences(initial)).toEqual([1n]);
      await appendFile(log, frame(2n));
      const appended = await page.evaluate(() =>
        (window as unknown as GatewayRenderer).gatewayRead(),
      );
      expect(sequences(appended)).toEqual([2n]);
      await page.evaluate(() => (window as unknown as GatewayRenderer).gatewayCancel());
      await app.evaluate(async () => {
        await (globalThis as typeof globalThis & {vibesysGatewayClosed: Promise<void>})
          .vibesysGatewayClosed;
      });
    } finally {
      watcher.close();
      serve.kill();
      await serveEnded;
      try {
        if (app !== null) await app.close();
      } finally {
        await rm(root, {recursive: true, force: true});
      }
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
  // Three full cold transfers are deliberately paced; this bound only guards a hang.
  test.setTimeout(180_000);
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

for (const damage of ['truncated', 'corrupt'] as const) {
  test(`SSH provisioning preserves the installed binary after a ${damage} upload`, async () => {
    const temporaryHome = await mkdtemp('/tmp/vsg-provision-');
    const bytes = Buffer.from('a complete verified gateway artifact');
    const destination = join(temporaryHome, GATEWAY_DIRECTORY, 'vibesysd');
    const link = new SshLink({
      alias: 'host-a',
      controlPath: '/tmp/vsg-control',
      askpass: '/tmp/vsg-askpass',
      scheduleTimeout: schedule,
      spawn: (_command, args, env) => {
        const child = spawn('/bin/sh', ['-c', args.at(-1) as string], {
          stdio: 'pipe',
          env: {...process.env, ...env, HOME: temporaryHome},
        });
        const transfer = new Transform({
          transform(chunk: Buffer, _encoding, callback) {
            const changed = Buffer.from(chunk);
            if (damage === 'corrupt') changed[0] = (changed[0] ?? 0) ^ 1;
            callback(null, damage === 'truncated' ? changed.subarray(0, 1) : changed);
          },
        });
        transfer.pipe(child.stdin);
        return {...fromChild(child), stdin: transfer};
      },
    });
    try {
      await mkdir(join(temporaryHome, GATEWAY_DIRECTORY), {recursive: true});
      await writeFile(destination, bytes);
      const original = await stat(destination);
      await expect(
        provisionGateway(link, async () => bytes, true, new AbortController().signal, schedule),
      ).rejects.toThrow(/gateway (artifact verification|command failed)/);
      expect((await readFile(destination)).equals(bytes)).toBe(true);
      expect((await stat(destination)).ino).toBe(original.ino);
      expect(await readdir(join(temporaryHome, GATEWAY_DIRECTORY))).toEqual(['vibesysd']);
    } finally {
      await rm(temporaryHome, {recursive: true, force: true});
    }
  });
}

interface GatewayRenderer {
  vibesysGateway: import('@vibesys/ipc').GatewayBridge;
  gatewayRead(): Promise<number[]>;
  gatewayCancel(): void;
}
function sequences(bytes: number[]): bigint[] {
  return fromBinary(TailResponseSchema, new Uint8Array(bytes)).records.map(
    bytes => fromBinary(RecordSchema, bytes).seq,
  );
}

async function startTail(page: Page): Promise<void> {
  await page.evaluate(
    request => {
      const target = window as unknown as GatewayRenderer;
      const queued: number[][] = [];
      const waiting: Array<{resolve(bytes: number[]): void; reject(error: unknown): void}> = [];
      let ended: string | undefined;
      target.gatewayRead = () => {
        const bytes = queued.shift();
        if (bytes !== undefined) return Promise.resolve(bytes);
        if (ended !== undefined) return Promise.reject(new Error(ended));
        return new Promise((resolve, reject) => waiting.push({resolve, reject}));
      };
      const call = target.vibesysGateway.stream(
        'local',
        request.method,
        new Uint8Array(request.bytes),
        bytes => {
          const waiter = waiting.shift();
          if (waiter === undefined) queued.push([...bytes]);
          else waiter.resolve([...bytes]);
        },
        error => {
          ended = error ?? 'stream ended';
          for (const waiter of waiting.splice(0)) waiter.reject(new Error(ended));
        },
      );
      target.gatewayCancel = () => call.cancel();
    },
    {
      method: TAIL_METHOD,
      bytes: [
        ...toBinary(TailRequestSchema, create(TailRequestSchema, {runId: 'run-a', fromSeq: 1n})),
      ],
    },
  );
}
