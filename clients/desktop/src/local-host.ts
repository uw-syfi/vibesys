/**
 * `LocalHost`: the `Host` role on this machine.
 *
 * It starts servers the way the TUI launcher does (`python -m entrypoints.server ARGS
 * --control-socket <session dir>/control.sock`, see `clients/tui/src/launcher.ts`), dials their
 * Unix sockets, and runs the `vibesys` command line. Every operating-system effect goes through the
 * `LocalSystem` seam, so the contract suite runs against it with in-memory processes and sockets.
 */
import {type ChildProcess, spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {createConnection} from 'node:net';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import type {Duplex, Readable} from 'node:stream';
import type {ScheduleTimeout} from '@vibesys/backend-client';
import {type Endpoint, type Host, HostError, type ServerExit, type ServerHandle} from './host.js';

/** A started process, as much of it as the host observes. */
export interface ProcessLike {
  readonly stdout: Readable;
  readonly stderr: Readable;
  /** Settles with the exit code; null when the process ended by signal or never started. */
  readonly exit: Promise<number | null>;
  kill(signal: 'SIGTERM' | 'SIGKILL'): void;
}

/** The operating-system effects `LocalHost` performs. */
export interface LocalSystem {
  /** Connect to a Unix socket; rejects when nothing accepts there. */
  connect(path: string): Promise<Duplex>;
  spawn(command: string, args: readonly string[]): ProcessLike;
  /** A fresh private directory for one server's socket. */
  makeSessionDirectory(): Promise<string>;
  removeDirectory(path: string): Promise<void>;
  readonly scheduleTimeout: ScheduleTimeout;
}

export interface LocalHostOptions {
  /** The argv prefix that runs Python with VibeSys importable, e.g. `['python3']`. */
  readonly python: readonly string[];
  readonly system?: LocalSystem;
  /** How long a started server may take to accept connections. */
  readonly readyTimeoutMs?: number;
  readonly readyPollMs?: number;
  /** How long a stopped server may take to exit after SIGTERM before it is killed. */
  readonly stopGraceMs?: number;
}

const DEFAULT_READY_TIMEOUT_MS = 30_000;
const DEFAULT_READY_POLL_MS = 50;
const DEFAULT_STOP_GRACE_MS = 5_000;
const LOG_TAIL_LINES = 20;

export class LocalHost implements Host {
  readonly #python: readonly string[];
  readonly #system: LocalSystem;
  readonly #readyAttempts: number;
  readonly #readyPollMs: number;
  readonly #stopGraceMs: number;
  readonly #streams = new Set<Duplex>();
  readonly #servers = new Set<ServerHandle>();
  #closed = false;

  constructor(options: LocalHostOptions) {
    const [command] = options.python;
    if (command === undefined) throw new Error('LocalHost needs a Python command');
    this.#python = options.python;
    this.#system = options.system ?? nodeSystem;
    this.#readyPollMs = options.readyPollMs ?? DEFAULT_READY_POLL_MS;
    this.#readyAttempts = Math.max(
      1,
      Math.ceil((options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS) / this.#readyPollMs),
    );
    this.#stopGraceMs = options.stopGraceMs ?? DEFAULT_STOP_GRACE_MS;
  }

  async startServer(args: readonly string[]): Promise<ServerHandle> {
    this.#assertOpen();
    const directory = await this.#system.makeSessionDirectory();
    const socketPath = join(directory, 'control.sock');
    const process = this.#run([
      '-m',
      'entrypoints.server',
      ...args,
      '--control-socket',
      socketPath,
    ]);
    const log = new LogTail(process);
    let stopping: Promise<void> | null = null;
    const exited: Promise<ServerExit> = process.exit.then(async code => {
      await this.#system.removeDirectory(directory);
      return {code, logTail: log.text()};
    });
    const handle: ServerHandle = {
      endpoint: {socketPath},
      exited,
      stop: () => {
        stopping ??= this.#stop(process, exited);
        return stopping;
      },
    };
    this.#servers.add(handle);
    void exited.then(() => this.#servers.delete(handle));
    try {
      await this.#waitUntilListening(socketPath, exited);
    } catch (error) {
      await handle.stop();
      throw error;
    }
    return handle;
  }

  async dial(endpoint: Endpoint): Promise<Duplex> {
    this.#assertOpen();
    let stream: Duplex;
    try {
      stream = await this.#system.connect(endpoint.socketPath);
    } catch (error) {
      throw new HostError('unreachable', `nothing is listening at ${endpoint.socketPath}`, {
        cause: error,
      });
    }
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
    const process = this.#run(['-m', 'entrypoints.launcher', ...argv]);
    const [stdout, stderr, code] = await Promise.all([
      collect(process.stdout),
      collect(process.stderr),
      process.exit,
    ]);
    if (code !== 0) {
      throw new HostError(
        'failed',
        `vibesys ${argv.join(' ')} exited with ${code ?? 'a signal'}: ${stderr.trim()}`,
      );
    }
    try {
      return JSON.parse(stdout) as unknown;
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
    await Promise.all([...this.#servers].map(server => server.stop()));
  }

  #run(args: readonly string[]): ProcessLike {
    const [command, ...prefix] = this.#python;
    // The constructor rejects an empty Python command.
    return this.#system.spawn(command as string, [...prefix, ...args]);
  }

  async #waitUntilListening(socketPath: string, exited: Promise<ServerExit>): Promise<void> {
    for (let attempt = 0; attempt < this.#readyAttempts; attempt += 1) {
      if (this.#closed) throw closedError();
      try {
        const probe = await this.#system.connect(socketPath);
        probe.destroy();
        return;
      } catch {
        // Not listening yet; the server's exit or the deadline decides below.
      }
      // An exit that has already happened wins the race: it is the first argument.
      const settled = await Promise.race([
        exited,
        delay(this.#readyPollMs, this.#system.scheduleTimeout),
      ]);
      if (settled !== undefined) throw startupFailure(settled);
    }
    throw new HostError('failed', `the server did not listen at ${socketPath} in time`);
  }

  async #stop(process: ProcessLike, exited: Promise<ServerExit>): Promise<void> {
    process.kill('SIGTERM');
    const cancel = this.#system.scheduleTimeout(() => process.kill('SIGKILL'), this.#stopGraceMs);
    await exited;
    cancel();
  }

  #assertOpen(): void {
    if (this.#closed) throw closedError();
  }
}

function closedError(): HostError {
  return new HostError('closed', 'the host is closed');
}

function startupFailure(exit: ServerExit): HostError {
  const tail = exit.logTail === '' ? '' : `\n${exit.logTail}`;
  return new HostError('failed', `the server exited with ${exit.code ?? 'a signal'}${tail}`);
}

function delay(ms: number, scheduleTimeout: ScheduleTimeout): Promise<undefined> {
  return new Promise(resolve => scheduleTimeout(() => resolve(undefined), ms));
}

/** The last lines a process wrote to either stream. */
class LogTail {
  #lines: string[] = [];
  #partial = '';

  constructor(process: ProcessLike) {
    for (const stream of [process.stdout, process.stderr]) {
      stream.setEncoding('utf8');
      stream.on('data', (chunk: string) => this.#push(chunk));
    }
  }

  text(): string {
    return [...this.#lines, this.#partial].filter(line => line !== '').join('\n');
  }

  #push(chunk: string): void {
    const lines = `${this.#partial}${chunk}`.split('\n');
    this.#partial = lines.pop() ?? '';
    this.#lines = [...this.#lines, ...lines].slice(-LOG_TAIL_LINES);
  }
}

async function collect(stream: Readable): Promise<string> {
  stream.setEncoding('utf8');
  let text = '';
  for await (const chunk of stream) text += chunk as string;
  return text;
}

/** The real operating system. */
const nodeSystem: LocalSystem = {
  connect: path =>
    new Promise((resolve, reject) => {
      const socket = createConnection(path);
      socket.once('connect', () => {
        socket.off('error', reject);
        resolve(socket);
      });
      socket.once('error', reject);
    }),
  spawn: (command, args) => fromChild(spawn(command, args, {stdio: ['ignore', 'pipe', 'pipe']})),
  makeSessionDirectory: () => mkdtemp(join(tmpdir(), 'vibesys-desktop-')),
  removeDirectory: path => rm(path, {recursive: true, force: true}),
  scheduleTimeout: (callback, ms) => {
    const timer = setTimeout(callback, ms);
    return () => clearTimeout(timer);
  },
};

function fromChild(child: ChildProcess): ProcessLike {
  const exit = new Promise<number | null>(resolve => {
    child.once('error', () => resolve(null));
    child.once('close', code => resolve(code));
  });
  return {
    // `stdio: 'pipe'` guarantees both streams.
    stdout: child.stdout as Readable,
    stderr: child.stderr as Readable,
    exit,
    kill: signal => {
      if (child.exitCode === null && child.signalCode === null) child.kill(signal);
    },
  };
}
