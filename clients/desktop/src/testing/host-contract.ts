/**
 * The `Host` contract suite. Every implementation (FakeHost, LocalHost, and later SshHost) runs
 * every case here through a `HostWorld`: the host under test plus the scripts that decide what its
 * servers and commands do, wired through whatever seam that implementation exposes.
 */
import {describe, expect, test} from 'bun:test';
import type {Duplex} from 'node:stream';
import {type Host, HostError, type HostErrorKind} from '../host.js';
import type {CommandResult, ServerScript} from './fake-host.js';

export interface HostWorld {
  readonly host: Host;
  /** Decide what each later `startServer(args)` runs. */
  scriptServer(script: (args: readonly string[]) => ServerScript): void;
  /** Decide what each later `invoke(argv)` prints. */
  scriptCommand(command: (argv: readonly string[]) => CommandResult): void;
}

/** A server that writes back every byte it reads. */
function echo(connection: Duplex): void {
  connection.pipe(connection);
}

async function rejection(promise: Promise<unknown>): Promise<HostError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof HostError) return error;
    throw error;
  }
  throw new Error('expected a HostError rejection');
}

async function expectKind(promise: Promise<unknown>, kind: HostErrorKind): Promise<HostError> {
  const error = await rejection(promise);
  expect(error.kind).toBe(kind);
  return error;
}

/** The first `length` characters the stream yields. */
function read(stream: Duplex, length: number): Promise<string> {
  stream.setEncoding('utf8');
  return new Promise((resolve, reject) => {
    let text = '';
    const onData = (chunk: string): void => {
      text += chunk;
      if (text.length < length) return;
      stream.off('data', onData);
      stream.pause();
      resolve(text.slice(0, length));
    };
    stream.on('data', onData);
    stream.once('error', reject);
  });
}

function closed(stream: Duplex): Promise<void> {
  if (stream.destroyed) return Promise.resolve();
  return new Promise(resolve => stream.once('close', () => resolve()));
}

export function describeHostContract(name: string, makeWorld: () => HostWorld): void {
  describe(`Host contract (${name})`, () => {
    test('a started server receives the caller arguments and its stream carries bytes both ways', async () => {
      const world = makeWorld();
      const seen: (readonly string[])[] = [];
      world.scriptServer(args => {
        seen.push(args);
        return echo;
      });
      const server = await world.host.startServer(['--project', '/p', '--', 'x y']);
      expect(seen).toEqual([['--project', '/p', '--', 'x y']]);
      const stream = await world.host.dial(server.endpoint);
      const reply = read(stream, 10);
      stream.write('{"a":"é"}\n');
      expect(await reply).toBe('{"a":"é"}\n');
      await world.host.close();
    });

    test('each dial is an independent connection', async () => {
      const world = makeWorld();
      let connections = 0;
      world.scriptServer(() => connection => {
        connections += 1;
        connection.end(`${connections}\n`);
      });
      const server = await world.host.startServer([]);
      // A host may probe the server before `startServer` resolves, so only distinctness counts.
      const first = await read(await world.host.dial(server.endpoint), 2);
      const second = await read(await world.host.dial(server.endpoint), 2);
      expect(second).not.toBe(first);
      await world.host.close();
    });

    test('a server that exits before it listens fails the start with its log', async () => {
      const world = makeWorld();
      world.scriptServer(() => ({code: 3, logTail: 'vibesys: no such project'}));
      const error = await expectKind(world.host.startServer([]), 'failed');
      expect(error.message).toContain('vibesys: no such project');
      await world.host.close();
    });

    test('stopping a server ends its streams, settles its exit, and makes it unreachable', async () => {
      const world = makeWorld();
      world.scriptServer(() => echo);
      const server = await world.host.startServer([]);
      const stream = await world.host.dial(server.endpoint);
      stream.resume();
      await server.stop();
      await server.exited;
      await closed(stream);
      await expectKind(world.host.dial(server.endpoint), 'unreachable');
      await world.host.close();
    });

    test('invoke parses the command output as JSON', async () => {
      const world = makeWorld();
      const seen: (readonly string[])[] = [];
      world.scriptCommand(argv => {
        seen.push(argv);
        return {code: 0, stdout: '{"instances": [{"id": "r1"}]}\n', stderr: ''};
      });
      expect(await world.host.invoke(['instances', 'list', '--json'])).toEqual({
        instances: [{id: 'r1'}],
      });
      expect(seen).toEqual([['instances', 'list', '--json']]);
      await world.host.close();
    });

    test('invoke reports a failed command with its standard error', async () => {
      const world = makeWorld();
      world.scriptCommand(() => ({code: 2, stdout: '', stderr: 'unknown command\n'}));
      const error = await expectKind(world.host.invoke(['nope']), 'failed');
      expect(error.message).toContain('unknown command');
      await world.host.close();
    });

    test('invoke reports output that is not JSON as malformed', async () => {
      const world = makeWorld();
      world.scriptCommand(() => ({code: 0, stdout: 'hello', stderr: ''}));
      await expectKind(world.host.invoke(['x']), 'malformed');
      await world.host.close();
    });

    test('close ends open streams, stops started servers, and refuses later work', async () => {
      const world = makeWorld();
      world.scriptServer(() => echo);
      const server = await world.host.startServer([]);
      const stream = await world.host.dial(server.endpoint);
      stream.resume();
      await world.host.close();
      await closed(stream);
      await server.exited;
      await expectKind(world.host.dial(server.endpoint), 'closed');
      await expectKind(world.host.startServer([]), 'closed');
      await expectKind(world.host.invoke(['x']), 'closed');
      await world.host.close();
    });
  });
}
