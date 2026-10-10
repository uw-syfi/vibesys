/**
 * `LocalHost`: the `Host` role on this machine.
 *
 * It is a `DetachedHost` whose access runs `vibesys` as `python -m entrypoints.launcher ARGV` and
 * dials Unix sockets directly, so a local run is started and found exactly as a remote one is:
 * `vibesys --detach`, then the record's socket. Every operating-system effect goes through the
 * `LocalSystem` seam, so the contract suite runs against it with in-memory processes and sockets.
 */
import {spawn} from 'node:child_process';
import {createConnection} from 'node:net';
import {homedir} from 'node:os';
import type {Duplex} from 'node:stream';
import {DetachedHost, type HostAccess} from './detached-host.js';
import {HostError} from './host.js';
import {type CommandOutput, finish, fromChild, type SpawnedProcess} from './process.js';
import {checkoutProblem, localCheckArgs, PROBED_DIRECTORIES} from './ssh-host.js';

/** The operating-system effects `LocalHost` performs. */
export interface LocalSystem {
  /** Connect to a Unix socket; rejects when nothing accepts there. */
  connect(path: string): Promise<Duplex>;
  /** Start `command args` in `cwd` (this process's directory when undefined). */
  spawn(command: string, args: readonly string[], cwd: string | undefined): SpawnedProcess;
}

export interface LocalHostOptions {
  /** The argv prefix that runs Python with VibeSys importable, e.g. `['python3']`. */
  readonly python: readonly string[];
  readonly system?: LocalSystem;
}

export class LocalHost extends DetachedHost {
  constructor(options: LocalHostOptions) {
    super(new LocalAccess(options.python, options.system ?? nodeSystem));
  }
}

class LocalAccess implements HostAccess {
  readonly #python: readonly [string, ...string[]];
  readonly #system: LocalSystem;

  constructor(python: readonly string[], system: LocalSystem) {
    const [command, ...prefix] = python;
    if (command === undefined) throw new Error('LocalHost needs a Python command');
    this.#python = [command, ...prefix];
    this.#system = system;
  }

  async ensureLink(): Promise<void> {}

  run(argv: readonly string[], cwd: string | undefined): Promise<CommandOutput> {
    const [command, ...prefix] = this.#python;
    return finish(
      this.#system.spawn(command, [...prefix, '-m', 'entrypoints.launcher', ...argv], cwd),
    );
  }

  async connect(socketPath: string): Promise<Duplex> {
    try {
      return await this.#system.connect(socketPath);
    } catch (error) {
      throw new HostError('unreachable', `nothing is listening at ${socketPath}`, {cause: error});
    }
  }

  async close(): Promise<void> {}
}

/** A checkout on this machine, checked: its physical path and the `uv` that runs it. */
export interface LocalCheckout {
  readonly root: string;
  readonly uv: string;
}

/**
 * Check `checkout` on this machine with the same scripts an SSH host runs (`/bin/sh`), and find
 * `uv`. Rejects with `failed` naming exactly what is missing.
 */
export async function checkLocalCheckout(
  checkout: string,
  system: LocalSystem = nodeSystem,
): Promise<LocalCheckout> {
  const where = `${checkout} on This Mac`;
  const verify = await finish(
    system.spawn('/bin/sh', localCheckArgs('verify', checkout), undefined),
  );
  const verdict = verify.stdout.split('\n')[0]?.trim() ?? '';
  if (verify.code !== 0 || !verdict.startsWith('root ')) {
    throw new HostError('failed', checkoutProblem(verdict, where, verify.stderr));
  }
  const probe = await finish(system.spawn('/bin/sh', localCheckArgs('probe', 'uv'), undefined));
  const uv = probe.stdout
    .split('\n')
    .find(line => line.startsWith('found '))
    ?.slice('found '.length);
  if (probe.code !== 0 || uv === undefined) {
    const tried = [
      "this app's PATH",
      "the login shell's PATH",
      ...PROBED_DIRECTORIES.map(d => `${d}/uv`),
    ];
    throw new HostError(
      'failed',
      `uv was not found on This Mac; tried ${tried.join(', ')}. Install uv (https://docs.astral.sh/uv/).`,
    );
  }
  return {root: verdict.slice('root '.length), uv};
}

/** How `LocalHost` runs Python from a checked checkout: `uv run --project ROOT python`. */
export function checkoutPython(checkout: LocalCheckout): string[] {
  return [checkout.uv, 'run', '--project', checkout.root, 'python'];
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
  spawn: (command, args, cwd) =>
    fromChild(
      spawn(command, args, {
        stdio: 'pipe',
        env: {...process.env, HOME: process.env['HOME'] ?? homedir()},
        ...(cwd === undefined ? {} : {cwd}),
      }),
    ),
};
