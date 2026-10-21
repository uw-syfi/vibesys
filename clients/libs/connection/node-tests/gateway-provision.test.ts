/** Deterministic pipe/clock tests of the same provisioner Electron main calls. */
import {test} from 'node:test';
import {Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {GatewayConnection, HELLO_METHOD, provisionGateway} from '../src/index.js';
import {fakeProcess} from '../src/testing/index.js';

type Command = ReturnType<typeof fakeProcess>;

/** Each spawn is a fresh in-memory process; the test supplies the remote process's results. */
class Commands {
  readonly #pending: Command[] = [];
  #arrived = (): void => {};
  count = 0;

  readonly spawnCommand = (_script: string) => {
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
    const bytes = Uint8Array.from({length}, (_, index) => index % 251);
    const {commands, clock, abort, outcome, upload} = await cold(bytes);
    try {
      if (length > 0)
        expect(clock.pendingDelays()).toContain(
          Math.ceil((1000 * Math.min(length, 32 * 1024)) / (512 * 1024)),
        );
      const received = receive(upload, clock);
      await send(clock, length);
      (await commands.next()).end(0, 'match');
      expect(await outcome).toBe('ok');
      expect(Buffer.concat(received.chunks).equals(Buffer.from(bytes))).toBe(true);
      const minimumMs = (1000 * length) / (512 * 1024);
      expect(clock.now()).toBeGreaterThanOrEqual(minimumMs);
      expect(clock.now()).toBeLessThanOrEqual(minimumMs + received.chunks.length);
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
  const {commands, clock, abort, outcome, upload} = await cold(new Uint8Array(64 * 1024), true);
  try {
    const received = receive(upload, clock);
    await send(clock, 64 * 1024);
    (await commands.next()).end(0, 'match');
    expect(await outcome).toBe('ok');
    expect(received.arrivals).toEqual([63, 126]);
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
  const {clock, abort, outcome, upload} = await cold(new Uint8Array(256 * 1024));
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
    const {commands, clock, abort, outcome, upload} = await cold(new Uint8Array(256 * 1024));
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
    const {commands, clock, abort, outcome, upload} = await cold(new Uint8Array(256 * 1024));
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
            async () => new Uint8Array(1024 * 1024),
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
      .unary(HELLO_METHOD, new Uint8Array(), abort.signal)
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
