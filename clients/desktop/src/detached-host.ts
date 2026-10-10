/**
 * `DetachedHost`: the `Host` role over detached runs and the live registry, for any machine.
 *
 * What differs between this machine and an SSH host is only how a `vibesys` command runs there and
 * how a Unix socket there is opened: that is the `HostAccess` interface (`LocalHost` and `SshHost`
 * supply one each). Everything else (starting a run with `vibesys --detach`, reading its record,
 * stopping it with `vibesys instances stop`, tracking streams for `close`) is here, once. Servers are
 * detached: closing the host ends its streams and its link, never a run.
 */
import type {Duplex} from 'node:stream';
import {
  type Endpoint,
  type Host,
  HostError,
  type ServerExit,
  type ServerHandle,
  type StartOptions,
} from './host.js';
import {type InstanceRecord, parseInstanceRecord, RecordError} from './instances.js';
import type {CommandOutput} from './process.js';

/** How a host runs `vibesys` and reaches a Unix socket on its machine. */
export interface HostAccess {
  /** See `Host.ensureLink`. */
  ensureLink(): Promise<void>;
  /**
   * Run `vibesys argv` to completion, in `cwd` when given. Rejects with `link` or `auth` when the
   * machine cannot be reached; a command that ran and failed resolves with its exit code.
   */
  run(argv: readonly string[], cwd: string | undefined): Promise<CommandOutput>;
  /** See `Host.dial`. */
  connect(socketPath: string): Promise<Duplex>;
  /** Release the access itself (an SSH master connection); streams are already destroyed. */
  close(): Promise<void>;
}

const LOG_TAIL_LINES = 20;

export class DetachedHost implements Host {
  readonly #access: HostAccess;
  readonly #streams = new Set<Duplex>();
  #closed = false;

  constructor(access: HostAccess) {
    this.#access = access;
  }

  async ensureLink(): Promise<void> {
    this.#assertOpen();
    await this.#access.ensureLink();
  }

  async startServer(args: readonly string[], options: StartOptions = {}): Promise<ServerHandle> {
    this.#assertOpen();
    const output = await this.#access.run(['--detach', ...args], options.cwd);
    if (output.code !== 0) {
      const tail = logTail(`${output.stdout}\n${output.stderr}`);
      throw new HostError(
        'failed',
        `the server exited with ${output.code ?? 'a signal'}${tail === '' ? '' : `\n${tail}`}`,
      );
    }
    const record = detachedRecord(output.stdout);
    const id = record.kind === 'compatible' ? record.instance.id : record.id;
    const socketPath = record.kind === 'compatible' ? record.instance.socketPath : '';
    let resolveExit: (exit: ServerExit) => void = () => {};
    const exited = new Promise<ServerExit>(resolve => {
      resolveExit = resolve;
    });
    let stopping: Promise<void> | null = null;
    const handle: ServerHandle = {
      endpoint: {socketPath},
      record,
      exited,
      stop: () => {
        stopping ??= this.#stop(id).then(exit => {
          resolveExit(exit);
        });
        return stopping;
      },
    };
    return handle;
  }

  async dial(endpoint: Endpoint): Promise<Duplex> {
    this.#assertOpen();
    const stream = await this.#access.connect(endpoint.socketPath);
    if (this.#closed) {
      stream.destroy();
      throw closedError();
    }
    this.#streams.add(stream);
    stream.once('close', () => this.#streams.delete(stream));
    return stream;
  }

  async invoke(argv: readonly string[]): Promise<unknown> {
    this.#assertOpen();
    const output = await this.#access.run(argv, undefined);
    if (output.code !== 0) {
      throw new HostError(
        'failed',
        `vibesys ${argv.join(' ')} exited with ${output.code ?? 'a signal'}: ${output.stderr.trim()}`,
      );
    }
    try {
      return JSON.parse(output.stdout) as unknown;
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
    await this.#access.close();
  }

  /** Stop registry instance `id`; a server that is already gone counts as stopped. */
  async #stop(id: string | null): Promise<ServerExit> {
    if (id === null) return {code: null, logTail: ''};
    let output: CommandOutput;
    try {
      output = await this.#access.run(['instances', 'stop', id, '--json'], undefined);
    } catch (error) {
      return {code: null, logTail: (error as Error).message};
    }
    return {code: output.code, logTail: logTail(output.stderr)};
  }

  #assertOpen(): void {
    if (this.#closed) throw closedError();
  }
}

/** The record `vibesys --detach` printed: its last non-empty line of standard output. */
function detachedRecord(stdout: string): InstanceRecord {
  const line = stdout
    .split('\n')
    .map(text => text.trim())
    .filter(text => text !== '')
    .at(-1);
  if (line === undefined) throw new HostError('malformed', 'vibesys --detach printed no record');
  try {
    return parseInstanceRecord(JSON.parse(line) as unknown);
  } catch (error) {
    const detail = error instanceof RecordError ? `: ${error.message}` : '';
    throw new HostError('malformed', `vibesys --detach did not print a record${detail}`, {
      cause: error,
    });
  }
}

function logTail(text: string): string {
  return text
    .split('\n')
    .filter(line => line.trim() !== '')
    .slice(-LOG_TAIL_LINES)
    .join('\n');
}

function closedError(): HostError {
  return new HostError('closed', 'the host is closed');
}
