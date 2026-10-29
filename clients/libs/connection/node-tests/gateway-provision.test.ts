/** Deterministic pipe/clock tests of the same provisioner Electron main calls. */
import {createHash} from 'node:crypto';
import {once} from 'node:events';
import {test} from 'node:test';
import {gunzipSync, gzipSync} from 'node:zlib';
import {create, toBinary} from '@bufbuild/protobuf';
import {API_SCHEMA_HASH, HelloRequestSchema} from '@vibesys/proto';
import {Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {
  GATEWAY_STDERR_BYTES,
  GatewayConnection,
  gatewayProcess,
  HELLO_METHOD,
  provisionGateway,
} from '../src/index.js';
import {fakeProcess, pipeHeldProcess} from '../src/testing/index.js';

type Command = ReturnType<typeof fakeProcess>;

/** Each spawn is a fresh in-memory process; the test supplies the remote process's results. */
class Commands {
  readonly #pending: Command[] = [];
  #arrived = (): void => {};
  count = 0;
  readonly scripts: string[] = [];

  readonly spawnCommand = (script: string) => {
    this.scripts.push(script);
    const child = fakeProcess();
    this.count += 1;
    this.#pending.push(child);
    this.#arrived();
    return child.process;
  };

  async next(): Promise<Command> {
    while (this.#pending.length === 0) {
      await new Promise<void>(resolve => {
        this.#arrived = resolve;
      });
    }
    const child = this.#pending.shift();
    if (child === undefined) throw new Error('missing command');
    return child;
  }
}

async function cold(bytes: Uint8Array, force = false) {
  const commands = new Commands();
  const clock = new FakeClock();
  const abort = new AbortController();
  const result = provisionGateway(commands, async () => bytes, force, abort.signal, clock.schedule);
  // Observe rejection immediately, including failures injected before the test awaits the result.
  const outcome = result.then(
    () => 'ok',
    error => String(error),
  );
  (await commands.next()).end(0, 'Linux\nx86_64\n');
  if (!force) (await commands.next()).end(0);
  const upload = await commands.next();
  return {commands, clock, abort, result, outcome, upload};
}

function receive(upload: Command, clock: FakeClock) {
  const chunks: Buffer[] = [];
  const arrivals: number[] = [];
  let previous = clock.now();
  upload.input.on('data', (chunk: Buffer) => {
    // A fragile route resets on a burst above 640 KiB/s. No real clock or network is involved.
    if (chunk.length > ((clock.now() - previous) * 640 * 1024) / 1000) {
      upload.input.destroy(new Error('ECONNRESET: upload burst'));
      return;
    }
    previous = clock.now();
    arrivals.push(previous);
    chunks.push(Buffer.from(chunk));
  });
  upload.input.on('finish', () => upload.end(0));
  return {chunks, arrivals};
}

async function send(clock: FakeClock, length: number): Promise<void> {
  for (let offset = 0; offset < length; offset += 32 * 1024) {
    const delay = Math.ceil((1000 * Math.min(32 * 1024, length - offset)) / (512 * 1024));
    await clock.waitUntilScheduled(delay);
    clock.advanceBy(delay);
  }
}

function randomBytes(length: number): Uint8Array {
  const random = new Seeded(length + 1);
  return Uint8Array.from({length}, () => random.int(256));
}

const random = new Seeded(1);
const lengths = [
  0,
  1,
  32 * 1024,
  32 * 1024 + 1,
  16_780_479,
  ...Array.from({length: 20}, () => random.int(256 * 1024)),
];
for (const length of lengths) {
  test(`cold upload of ${length} bytes stays paced on a burst-sensitive route`, {
    timeout: 15000,
  }, async () => {
    const bytes =
      length === 16_780_479
        ? Uint8Array.from({length}, (_, index) => index % 251)
        : randomBytes(length);
    const wireLength = gzipSync(bytes).length;
    const {commands, clock, abort, outcome, upload} = await cold(bytes);
    try {
      if (wireLength > 0)
        expect(clock.pendingDelays()).toContain(
          Math.ceil((1000 * Math.min(wireLength, 32 * 1024)) / (512 * 1024)),
        );
      const received = receive(upload, clock);
      await send(clock, wireLength);
      (await commands.next()).end(0, 'match');
      expect(await outcome).toBe('ok');
      expect(gunzipSync(Buffer.concat(received.chunks)).equals(Buffer.from(bytes))).toBe(true);
      const minimumMs = (1000 * wireLength) / (512 * 1024);
      expect(clock.now()).toBeGreaterThanOrEqual(minimumMs);
      expect(clock.now()).toBeLessThanOrEqual(minimumMs + received.chunks.length);
      expect(commands.scripts[2]).toContain('command -v gzip');
      expect(commands.scripts[2]).toContain('gzip -dc >');
      expect(commands.scripts[3]).toContain(String(bytes.length));
      expect(commands.scripts[3]).toContain(createHash('sha256').update(bytes).digest('hex'));
      if (length === 16_780_479) expect(wireLength).toBeLessThan(length / 100);
      expect(commands.count).toBe(4);
      expect(clock.pendingDelays()).toEqual([]);
    } finally {
      abort.abort();
      await outcome;
    }
  });
}

test('warm provisioning opens no upload or pacing timer', {timeout: 15000}, async () => {
  const commands = new Commands();
  const clock = new FakeClock();
  const result = provisionGateway(
    commands,
    async () => new Uint8Array(10),
    false,
    new AbortController().signal,
    clock.schedule,
  );
  (await commands.next()).end(0, 'Linux\narm64\n');
  (await commands.next()).end(0, 'match');
  await result;
  expect(commands.count).toBe(2);
  expect(clock.now()).toBe(0);
  expect(clock.pendingDelays()).toEqual([]);
});

test('a forced replacement uses the same paced transfer', {timeout: 15000}, async () => {
  const bytes = randomBytes(64 * 1024);
  const {commands, clock, abort, outcome, upload} = await cold(bytes, true);
  try {
    const received = receive(upload, clock);
    await send(clock, gzipSync(bytes).length);
    (await commands.next()).end(0, 'match');
    expect(await outcome).toBe('ok');
    expect(received.arrivals.slice(0, 2)).toEqual([63, 126]);
    expect(gunzipSync(Buffer.concat(received.chunks))).toEqual(Buffer.from(bytes));
    expect(commands.count).toBe(3);
    expect(clock.pendingDelays()).toEqual([]);
  } finally {
    abort.abort();
    await outcome;
  }
});

test('pipe backpressure delays the next pacing timer until the write completes', {
  timeout: 15000,
}, async () => {
  const {clock, abort, outcome, upload} = await cold(randomBytes(256 * 1024));
  try {
    // Do not read the pipe: its real in-memory high water mark eventually blocks the writer.
    for (let index = 0; index < 2; index += 1) {
      await clock.waitUntilScheduled(63);
      clock.advanceBy(63);
    }
    expect(clock.pendingDelays()).not.toContain(63);
    upload.input.resume();
    await clock.waitUntilScheduled(63);
    expect(clock.pendingDelays()).toContain(63);
  } finally {
    abort.abort();
    expect(await outcome).toContain('Abort');
    await upload.process.exit;
  }
  expect(clock.pendingDelays()).toEqual([]);
});

for (const sent of [0, 1, 3]) {
  test(`cancellation after ${sent} chunks cancels pacing and retires the upload`, {
    timeout: 15000,
  }, async () => {
    const {commands, clock, abort, outcome, upload} = await cold(randomBytes(256 * 1024));
    const received = receive(upload, clock);
    await send(clock, sent * 32 * 1024);
    abort.abort();
    expect(await outcome).toContain('Abort');
    clock.advanceBy(180_000);
    expect(received.chunks).toHaveLength(sent);
    expect(upload.process.running).toBe(false);
    expect(commands.count).toBe(3);
    expect(clock.pendingDelays()).toEqual([]);
  });
}

for (const failure of ['pipe reset', 'SSH exit', 'timeout'] as const) {
  test(`${failure} stops writing and does not verify an unfinished upload`, {
    timeout: 15000,
  }, async () => {
    const {commands, clock, abort, outcome, upload} = await cold(randomBytes(256 * 1024));
    try {
      if (failure === 'pipe reset') upload.input.destroy(new Error('EPIPE'));
      else if (failure === 'SSH exit') upload.end(255, '', 'connection reset');
      else clock.advanceBy(90_000);
      expect(await outcome).toContain(
        failure === 'pipe reset' ? 'EPIPE' : failure === 'SSH exit' ? '255' : 'timed out',
      );
      clock.advanceBy(180_000);
      expect(upload.process.running).toBe(false);
      expect(commands.count).toBe(3);
      expect(clock.pendingDelays()).toEqual([]);
    } finally {
      abort.abort();
      await outcome;
    }
  });
}

for (const mode of ['slow upload', 'reset then retry'] as const) {
  test(`gateway connection owns a ${mode} through fresh command channels`, {
    timeout: 15000,
  }, async () => {
    const commands = new Commands();
    const clock = new FakeClock();
    const abort = new AbortController();
    const signals: AbortSignal[] = [];
    const connection = new GatewayConnection(
      {
        dial: async (signal, force) => {
          signals.push(signal);
          await provisionGateway(
            commands,
            async () => randomBytes(1024 * 1024),
            force,
            signal,
            clock.schedule,
          );
          throw new Error('test stops before opening the gateway RPC pipe');
        },
      },
      clock.schedule,
    );
    const result = connection
      .unary(
        HELLO_METHOD,
        toBinary(HelloRequestSchema, create(HelloRequestSchema, {apiSchemaHash: API_SCHEMA_HASH})),
        abort.signal,
      )
      .catch(error => String(error));
    let upload: Command | undefined;
    try {
      (await commands.next()).end(0, 'Linux\nx86_64\n');
      (await commands.next()).end(0);
      upload = await commands.next();
      if (mode === 'slow upload') {
        clock.advanceBy(30_001);
        expect(signals).toHaveLength(1);
        expect(signals[0]?.aborted).toBe(false);
        expect(upload.process.running).toBe(true);
      } else {
        const first = upload;
        receive(first, clock);
        await send(clock, 3 * 32 * 1024);
        first.input.destroy(new Error('EPIPE'));
        await clock.waitUntilScheduled(500);
        clock.advanceBy(500);
        (await commands.next()).end(0, 'Linux\nx86_64\n');
        (await commands.next()).end(0);
        upload = await commands.next();
        expect(upload).not.toBe(first);
        expect(first.process.running).toBe(false);
        expect(signals).toHaveLength(2);
        expect(commands.count).toBe(6);
        expect(clock.pendingDelays()).toContain(63);
      }
    } finally {
      abort.abort();
      connection.close();
      expect(await result).toContain('Abort');
      await upload?.process.exit;
    }
    expect(clock.pendingDelays()).toEqual([]);
  });
}

for (const error of ['gateway provisioning requires gzip', 'gzip: unexpected end of file']) {
  test(`remote decompression failure is reported: ${error}`, {timeout: 15000}, async () => {
    const {commands, clock, outcome, upload} = await cold(new Uint8Array(1024));
    upload.end(1, '', error);
    expect(await outcome).toContain(error);
    expect(commands.count).toBe(3);
    expect(clock.pendingDelays()).toEqual([]);
  });
}

test('verification rejects a decompressed artifact that fails the original digest', {
  timeout: 15000,
}, async () => {
  const bytes = randomBytes(1024);
  const {commands, clock, outcome, upload} = await cold(bytes);
  receive(upload, clock);
  await send(clock, gzipSync(bytes).length);
  (await commands.next()).end(0);
  expect(await outcome).toContain('gateway artifact verification failed');
  expect(clock.pendingDelays()).toEqual([]);
});

test('the connect process stderr reaches the app log as a bounded tail when it exits', {
  timeout: 15000,
}, async () => {
  const clock = new FakeClock();
  for (const size of [0, 10, GATEWAY_STDERR_BYTES, 3 * GATEWAY_STDERR_BYTES]) {
    // Pipes end before the exit, as a real child's do; the exit then destroys stdin.
    const child = pipeHeldProcess();
    const lines: string[] = [];
    let fail: (message: string) => void = () => {};
    const errored = new Promise<string>(resolve => {
      fail = resolve;
    });
    const logged = new Promise<void>(resolve => {
      const duplex = gatewayProcess(child.process, clock.schedule, line => {
        lines.push(line);
        resolve();
      });
      duplex.on('error', error => fail(error.message));
      duplex.resume();
    });
    child.stderr.end(`${'x'.repeat(size)}serve: listen failed`);
    child.stdout.end();
    await once(child.stdout, 'end');
    child.end(1);
    await logged;
    expect(lines).toHaveLength(1);
    const [line = ''] = lines;
    expect(line.startsWith('gateway connect exited: 1')).toBe(true);
    const message = await errored;
    expect(message.startsWith('gateway connect exited: 1: ')).toBe(true);
    expect(message.endsWith('serve: listen failed')).toBe(true);
    expect(line.endsWith('serve: listen failed')).toBe(true);
    expect(line.length).toBeLessThanOrEqual(
      'gateway connect exited: 1\n'.length + GATEWAY_STDERR_BYTES,
    );
  }
});

test("a channel that dies silently logs its code, the link's cause and that ssh said nothing", {
  timeout: 15000,
}, async () => {
  const clock = new FakeClock();
  for (const code of [255, 1, null]) {
    const child = pipeHeldProcess();
    const lines: string[] = [];
    let fail: (message: string) => void = () => {};
    const errored = new Promise<string>(resolve => {
      fail = resolve;
    });
    const logged = new Promise<void>(resolve => {
      const duplex = gatewayProcess(
        child.process,
        clock.schedule,
        line => {
          lines.push(line);
          resolve();
        },
        (exit, tail) => `link says ${exit} with ${tail.length} bytes`,
      );
      duplex.on('error', error => fail(error.message));
      duplex.resume();
    });
    child.stderr.end();
    child.stdout.end();
    await once(child.stdout, 'end');
    child.end(code);
    await logged;
    const [line = ''] = lines;
    expect(line).toBe(
      `gateway connect exited: ${code} (link says ${code} with 0 bytes)\nno output from ssh`,
    );
    expect(await errored).toBe(
      `gateway connect exited: ${code}: link says ${code} with 0 bytes: no output from ssh`,
    );
  }
});
