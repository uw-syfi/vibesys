/**
 * `FakeHost`: an in-memory `Host` for tests of whatever drives a host (the relay, the window
 * wiring). It passes the same contract suite as `LocalHost` (`testing/host-contract.ts`).
 *
 * Servers are connection handlers on a `FakeNetwork`; commands are scripted results. It is no more
 * forgiving than a real host: dialing a stopped server is `unreachable`, a failed command is
 * `failed`, and nothing works after `close()`.
 */
import type {Duplex} from 'node:stream';
import {type Endpoint, type Host, HostError, type ServerExit, type ServerHandle} from '../host.js';
import {parseInstanceRecord} from '../instances.js';
import {type ConnectionHandler, FakeNetwork} from './fake-network.js';
import {fakeRecord} from './fake-record.js';

/** What a scripted command prints and how it exits. */
export interface CommandResult {
  readonly code: number;
  readonly stdout: string;
  readonly stderr: string;
}

/** What a scripted server start does: serve connections, or exit before it ever listens. */
export type ServerScript = ConnectionHandler | ServerExit;

export interface FakeHostOptions {
  readonly network?: FakeNetwork;
  /** What `startServer(args)` runs. Defaults to a server that accepts and ignores connections. */
  readonly server?: (args: readonly string[]) => ServerScript;
  /** What `invoke(argv)` prints. Defaults to a command that does not exist (exit 127). */
  readonly command?: (argv: readonly string[]) => CommandResult;
}

export class FakeHost implements Host {
  readonly network: FakeNetwork;
  readonly #server: (args: readonly string[]) => ServerScript;
  readonly #command: (argv: readonly string[]) => CommandResult;
  readonly #streams = new Set<Duplex>();
  #nextServer = 0;
  #closed = false;

  constructor(options: FakeHostOptions = {}) {
    this.network = options.network ?? new FakeNetwork();
    this.#server = options.server ?? (() => () => {});
    this.#command =
      options.command ?? (argv => ({code: 127, stdout: '', stderr: `${argv[0]}: not found`}));
  }

  async ensureLink(): Promise<void> {
    this.#assertOpen();
  }

  async startServer(args: readonly string[]): Promise<ServerHandle> {
    this.#assertOpen();
    const script = this.#server(args);
    if (typeof script !== 'function') {
      const tail = script.logTail === '' ? '' : `\n${script.logTail}`;
      throw new HostError('failed', `the server exited with ${script.code ?? 'a signal'}${tail}`);
    }
    this.#nextServer += 1;
    const socketPath = `/fake/server-${this.#nextServer}/control.sock`;
    this.network.listen(socketPath, script);
    let resolveExit: (exit: ServerExit) => void = () => {};
    const exited = new Promise<ServerExit>(resolve => {
      resolveExit = resolve;
    });
    const id = this.#nextServer.toString(16).padStart(12, '0');
    const handle: ServerHandle = {
      endpoint: {socketPath},
      record: parseInstanceRecord(fakeRecord(id, socketPath)),
      exited,
      stop: async () => {
        this.network.unlisten(socketPath);
        resolveExit({code: null, logTail: ''});
        await exited;
      },
    };
    return handle;
  }

  async dial(endpoint: Endpoint): Promise<Duplex> {
    this.#assertOpen();
    const stream = await this.network.connect(endpoint.socketPath);
    this.#streams.add(stream);
    stream.once('close', () => this.#streams.delete(stream));
    return stream;
  }

  async invoke(argv: readonly string[]): Promise<unknown> {
    this.#assertOpen();
    const result = this.#command(argv);
    if (result.code !== 0) {
      throw new HostError(
        'failed',
        `vibesys ${argv.join(' ')} exited with ${result.code}: ${result.stderr.trim()}`,
      );
    }
    try {
      return JSON.parse(result.stdout) as unknown;
    } catch (error) {
      throw new HostError('malformed', `vibesys ${argv.join(' ')} did not print JSON`, {
        cause: error,
      });
    }
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    for (const stream of this.#streams) stream.destroy();
    this.#streams.clear();
  }

  #assertOpen(): void {
    if (this.#closed) throw new HostError('closed', 'the host is closed');
  }
}
