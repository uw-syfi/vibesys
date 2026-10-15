import {describe, expect, test} from 'bun:test';
import {Writable} from 'node:stream';
import {FakeClock} from '@vibesys/transport/testing';
import {ConnectionError, HostConnection} from '../index.js';
import {ECHO_SERVER, FakeLink, phaseReached, record} from '../testing/index.js';

const CONFIG = {delaysMs: [500], deadlineMs: 60_000, heartbeatMs: 15_000};
const INSTANCE_ID = '00000000000b';

type InputFault = 'throw' | 'callback' | 'event' | 'final';

interface ArmedFault {
  readonly received: Promise<void>;
  release(): void;
}

interface PendingFault {
  readonly kind: InputFault | 'hold';
  readonly received: () => void;
  readonly released: Promise<void>;
}

function pipeError(): Error & {code: string} {
  return Object.assign(new Error('broken pipe'), {code: 'EPIPE'});
}

/** A faithful Writable that can schedule each way Node reports a rejected process-input write. */
class FaultableInput extends Writable {
  readonly #target: Writable;
  #next: PendingFault | null = null;
  started = 0;

  constructor(target: Writable) {
    super({highWaterMark: 1});
    this.#target = target;
  }

  arm(kind: PendingFault['kind']): ArmedFault {
    if (this.#next !== null) throw new Error('an input fault is already armed');
    let received = (): void => {};
    let release = (): void => {};
    const entered = new Promise<void>(resolve => {
      received = resolve;
    });
    const released = new Promise<void>(resolve => {
      release = resolve;
    });
    this.#next = {kind, received, released};
    return {received: entered, release};
  }

  override write(
    chunk: Uint8Array | string,
    encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
    callback?: (error?: Error | null) => void,
  ): boolean {
    if (this.#next?.kind === 'throw') {
      const fault = this.#take('throw');
      fault.received();
      throw pipeError();
    }
    if (typeof encodingOrCallback === 'function') return super.write(chunk, encodingOrCallback);
    if (encodingOrCallback === undefined) return super.write(chunk);
    return super.write(chunk, encodingOrCallback, callback);
  }

  override _write(
    chunk: Buffer,
    encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    this.started += 1;
    const fault = this.#next;
    if (fault?.kind === 'callback' || fault?.kind === 'event' || fault?.kind === 'hold') {
      this.#next = null;
      fault.received();
      void fault.released.then(() => {
        if (fault.kind === 'callback') callback(pipeError());
        else if (fault.kind === 'event') this.emit('error', pipeError());
        else this.#target.write(chunk, encoding, callback);
      });
      return;
    }
    this.#target.write(chunk, encoding, callback);
  }

  override _final(callback: (error?: Error | null) => void): void {
    if (this.#next?.kind === 'final') {
      const fault = this.#take('final');
      fault.received();
      void fault.released.then(() => callback(pipeError()));
      return;
    }
    this.#target.end(callback);
  }

  override _destroy(error: Error | null, callback: (error?: Error | null) => void): void {
    this.#target.destroy();
    callback(error);
  }

  #take(kind: PendingFault['kind']): PendingFault {
    const fault = this.#next;
    if (fault?.kind !== kind) throw new Error(`expected an armed ${kind} fault`);
    this.#next = null;
    return fault;
  }
}

class FaultableLink extends FakeLink {
  readonly inputs: FaultableInput[] = [];

  override spawnHelper(checkout: string): ReturnType<FakeLink['spawnHelper']> {
    const process = super.spawnHelper(checkout);
    const stdin = new FaultableInput(process.stdin);
    this.inputs.push(stdin);
    return {...process, stdin};
  }
}

function connectionOver(link: FaultableLink): HostConnection {
  return new HostConnection({
    link,
    checkout: '/src/vibesys',
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
}

async function connectedRun(link: FaultableLink, connection: HostConnection) {
  link.helper.publish(record(INSTANCE_ID, 'run-b'));
  link.helper.host.runs.set(INSTANCE_ID, ECHO_SERVER);
  const opened = await connection.openRun(INSTANCE_ID);
  const input = link.inputs.at(-1);
  if (input === undefined) throw new Error('the run helper input was not created');
  return {...opened, input};
}

async function rejected(operation: Promise<unknown>): Promise<Error> {
  try {
    await operation;
  } catch (error) {
    return error as Error;
  }
  throw new Error('the operation resolved instead of rejecting');
}

function streamErrors(stream: NodeJS.ReadWriteStream): {
  readonly errors: Error[];
  readonly closed: Promise<void>;
} {
  const errors: Error[] = [];
  stream.on('error', error => errors.push(error));
  const closed = new Promise<void>(resolve => stream.once('close', resolve));
  return {errors, closed};
}

describe('helper process input failures', () => {
  test('a rejected control write promptly fails its request and retires the channel', async () => {
    const link = new FaultableLink();
    await using connection = connectionOver(link);
    await connection.ready();
    const input = link.inputs[0];
    if (input === undefined) throw new Error('the control helper input was not created');
    const fault = input.arm('callback');
    const reconnecting = phaseReached(connection, 'reconnecting');

    const request = connection.request({type: 'ping'});
    await fault.received;
    fault.release();
    const error = await rejected(request);

    expect(error).toBeInstanceOf(ConnectionError);
    expect((error as ConnectionError).reason).toBe('link');
    expect(error.message).toContain('broken pipe');
    await reconnecting;
    expect(connection.phase.kind).toBe('reconnecting');
    expect(link.helper.channels[0]?.process.running).toBe(false);
  });

  for (const kind of ['throw', 'callback', 'event'] as const) {
    test(`a run write reported by ${kind} settles once with a typed failure`, async () => {
      const link = new FaultableLink();
      await using connection = connectionOver(link);
      const {stream, input} = await connectedRun(link, connection);
      const observed = streamErrors(stream);
      const fault = input.arm(kind);
      let completions = 0;
      const completed = new Promise<Error>((resolve, reject) => {
        stream.write('lost\n', error => {
          completions += 1;
          if (error == null) reject(new Error('the rejected write reported success'));
          else resolve(error);
        });
      });

      await fault.received;
      fault.release();
      const error = await completed;
      await observed.closed;

      expect(error).toBeInstanceOf(ConnectionError);
      expect((error as ConnectionError).reason).toBe('link');
      expect(error.message).toContain('broken pipe');
      expect(completions).toBe(1);
      expect(observed.errors).toHaveLength(1);
      expect(observed.errors[0]).toBe(error);
    });
  }

  test('a rejected input finalization becomes the run stream terminal error', async () => {
    const link = new FaultableLink();
    await using connection = connectionOver(link);
    const {stream, input} = await connectedRun(link, connection);
    const observed = streamErrors(stream);
    const fault = input.arm('final');
    let completions = 0;
    const completed = new Promise<Error>((resolve, reject) => {
      stream.end((error?: Error | null) => {
        completions += 1;
        if (error == null) reject(new Error('the rejected finalization reported success'));
        else resolve(error);
      });
    });

    await fault.received;
    fault.release();
    const error = await completed;
    await observed.closed;

    expect(error).toBeInstanceOf(ConnectionError);
    expect((error as ConnectionError).reason).toBe('link');
    expect(error.message).toContain('broken pipe');
    expect(completions).toBe(1);
    expect(observed.errors).toEqual([error]);
  });

  test('backpressured successful run writes stay ordered and wait for input acceptance', async () => {
    const link = new FaultableLink();
    await using connection = connectionOver(link);
    const {stream, input} = await connectedRun(link, connection);
    const first = input.arm('hold');
    const completions: string[] = [];
    const started = input.started;

    stream.write('first\n', error => {
      expect(error).toBeNull();
      completions.push('first');
    });
    stream.write('second\n', error => {
      expect(error).toBeNull();
      completions.push('second');
    });
    await first.received;

    expect(input.writableNeedDrain).toBe(true);
    expect(input.started - started).toBe(1);
    expect(completions).toEqual([]);
    first.release();
    await new Promise<void>((resolve, reject) => {
      stream.end((error?: Error | null) => {
        if (error == null) resolve();
        else reject(error);
      });
    });

    expect(completions).toEqual(['first', 'second']);
    expect(input.started - started).toBe(2);
  });
});
