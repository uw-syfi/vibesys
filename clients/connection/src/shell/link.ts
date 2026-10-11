/**
 * How the app reaches one host: the `Link` role, and its two implementations.
 *
 * A link brings the way to the host up (`establish`) and starts one host helper per channel
 * (`spawnHelper`) from a checkout; everything after that is the helper's typed channel, the same
 * for every host. There is one link per host, whatever checkouts its connections run from: it owns
 * the host's ssh master and what the host's bootstraps reported (`heard`).
 *
 * - `LocalLink` (This Mac): no link to establish; each helper is a child process running the
 *   bootstrap with an argument vector.
 * - `SshLink`: the system OpenSSH client, so the user's `~/.ssh/config`, ProxyJump, agent,
 *   certificates, and Kerberos work unchanged. The app owns one master connection per host
 *   (`ControlMaster`/`ControlPath`/`ControlPersist`), and only the master authenticates: it is the
 *   one ssh started with an askpass helper, so a password or 2FA prompt appears once, as a dialog,
 *   and only when the user asked (`interactive`). It carries `ServerAliveInterval`, so a half-open
 *   link ends it instead of hanging, and `ControlPersist=30m`, so a master an app crash left behind
 *   ends on its own. Every helper is an exec channel over that master with `BatchMode=yes`: it never
 *   prompts.
 */
import {spawn} from 'node:child_process';
import {homedir} from 'node:os';
import {bootstrapUv, localBootstrap, remoteBootstrap} from './bootstrap.js';
import {ConnectionError} from './helper-channel.js';
import {fromChild, type SpawnedProcess} from './process.js';

export interface Link {
  /** The host, for messages: `This Mac`, or its ssh alias. */
  readonly host: string;
  /**
   * Bring the link up; only an `interactive` attempt (the user's own action) may prompt. Rejects
   * with a `ConnectionError`: `auth` when the host refused the user, `link` when it is unreachable.
   * Concurrent calls share one attempt. Aborting one caller stops waiting for it; the attempt is
   * stopped only when it has no callers left.
   */
  establish(interactive: boolean, signal?: AbortSignal): Promise<void>;
  /** Forget in-flight evidence after a network change; the next `establish` starts fresh. */
  networkChanged(): void;
  /** Start one helper on the host from `checkout`: a new channel. */
  spawnHelper(checkout: string): SpawnedProcess;
  /** A line a bootstrap printed before its helper answered; the link keeps what it reports. */
  heard(line: string): void;
  /** Why a helper's process for `where` (`CHECKOUT on HOST`) ended, when the helper did not say. */
  explain(code: number | null, stderr: string, where: string): ConnectionError;
  /** Release the link itself (the ssh master); channels are already ended. */
  release(): Promise<void>;
}

/** Runs a program with arguments and extra environment variables. */
export type Spawn = (
  command: string,
  args: readonly string[],
  env: Readonly<Record<string, string>>,
) => SpawnedProcess;

/** The real operating system. */
export const nodeSpawn: Spawn = (command, args, env) =>
  fromChild(
    spawn(command, args, {
      stdio: 'pipe',
      env: {...process.env, HOME: process.env['HOME'] ?? homedir(), ...env},
    }),
  );

function lastLine(text: string): string {
  return (
    text
      .split('\n')
      .map(line => line.trim())
      .filter(line => line !== '')
      .at(-1) ?? 'no detail'
  );
}

/** A helper that exited on its own without its report: its checkout cannot serve this app. */
function helperExit(code: number | null, stderr: string, where: string): ConnectionError {
  if (code === null) return new ConnectionError('link', `the helper for ${where} was stopped`);
  return new ConnectionError(
    'incompatible',
    `the host helper for ${where} exited with ${code}: ${lastLine(stderr)}`,
  );
}

export class LocalLink implements Link {
  readonly host = 'This Mac';
  readonly #spawn: Spawn;
  /** The `uv` a bootstrap found, passed to the next one. */
  #uv: string | null = null;

  constructor(run: Spawn = nodeSpawn) {
    this.#spawn = run;
  }

  async establish(): Promise<void> {}

  networkChanged(): void {}

  spawnHelper(checkout: string): SpawnedProcess {
    return this.#spawn('/bin/sh', localBootstrap(checkout, this.#uv), {});
  }

  heard(line: string): void {
    this.#uv = bootstrapUv(line) ?? this.#uv;
  }

  explain(code: number | null, stderr: string, where: string): ConnectionError {
    return helperExit(code, stderr, where);
  }

  async release(): Promise<void> {}
}

export interface SshLinkOptions {
  /** The `Host` alias from `~/.ssh/config` (validated by the settings). */
  readonly alias: string;
  /** The master connection's socket path on this machine; short (Unix sockets cap paths). */
  readonly controlPath: string;
  /** The askpass helper the master uses for password and 2FA prompts. */
  readonly askpass: string;
  readonly spawn?: Spawn;
}

/** ssh's own exit status for a connection or authentication failure. */
const SSH_FAILED = 255;

/**
 * ssh failures only the user can answer: refused credentials, or a host key that is not trusted yet
 * (BatchMode cannot ask; an interactive sign-in asks through the askpass dialog, never silently).
 */
const AUTH_FAILURE =
  /Permission denied|Too many authentication failures|Authentication failed|Host key verification failed/;

interface EstablishAttempt {
  process: SpawnedProcess | null;
  cancelled: boolean;
  readonly waiters: Set<object>;
  promise: Promise<void>;
}

export class SshLink implements Link {
  readonly host: string;
  readonly #options: SshLinkOptions;
  readonly #spawn: Spawn;
  /** Every establishment subprocess that has not exited, including retired generations. */
  readonly #attempts = new Set<EstablishAttempt>();
  #establishing: EstablishAttempt | null = null;
  #released = false;
  /** The `uv` a bootstrap on the host found, passed to the next one. */
  #uv: string | null = null;

  constructor(options: SshLinkOptions) {
    this.#options = options;
    this.#spawn = options.spawn ?? nodeSpawn;
    this.host = options.alias;
  }

  /** Establish the master if it is down; one attempt at a time. */
  establish(interactive: boolean, signal?: AbortSignal): Promise<void> {
    if (this.#released) return Promise.reject(this.#releasedError());
    if (signal?.aborted === true) {
      return Promise.reject(new ConnectionError('link', 'the connection attempt was abandoned'));
    }
    const attempt = this.#establishing ?? this.#begin(interactive);
    const waiter = {};
    attempt.waiters.add(waiter);
    return new Promise((resolve, reject) => {
      const stopWaiting = (): boolean => {
        if (!attempt.waiters.delete(waiter)) return false;
        signal?.removeEventListener('abort', aborted);
        return true;
      };
      const aborted = (): void => {
        if (!stopWaiting()) return;
        reject(new ConnectionError('link', 'the connection attempt was abandoned'));
        if (attempt.waiters.size === 0) this.#cancel(attempt);
      };
      signal?.addEventListener('abort', aborted, {once: true});
      void attempt.promise.then(
        () => {
          if (stopWaiting()) resolve();
        },
        error => {
          if (stopWaiting()) reject(error);
        },
      );
    });
  }

  networkChanged(): void {
    if (this.#establishing !== null) this.#cancel(this.#establishing);
  }

  spawnHelper(checkout: string): SpawnedProcess {
    return this.#ssh(
      ['-T', '-o', 'ControlMaster=no', '-o', 'BatchMode=yes', '--', this.#options.alias],
      remoteBootstrap(checkout, this.#uv),
    );
  }

  heard(line: string): void {
    this.#uv = bootstrapUv(line) ?? this.#uv;
  }

  explain(code: number | null, stderr: string, where: string): ConnectionError {
    if (code === SSH_FAILED) return this.#sshError(stderr);
    if (code === null)
      return new ConnectionError('link', `the channel to ${this.#options.alias} broke`);
    return helperExit(code, stderr, where);
  }

  async release(): Promise<void> {
    // Every active or retired attempt owns the same master this release closes. Kill each attempt
    // so quitting is not held hostage by ConnectTimeout, then wait for confirmed exits before
    // `-O exit`; otherwise a retired process could still fork a master after exit completed.
    this.#released = true;
    for (const attempt of this.#attempts) this.#cancel(attempt);
    await Promise.all([...this.#attempts].map(attempt => attempt.promise.catch(() => {})));
    await this.#finish(this.#ssh(['-O', 'exit', '--', this.#options.alias]));
  }

  #begin(interactive: boolean): EstablishAttempt {
    const attempt: EstablishAttempt = {
      process: null,
      cancelled: false,
      waiters: new Set(),
      promise: Promise.resolve(),
    };
    this.#establishing = attempt;
    attempt.promise = this.#establish(interactive, attempt).finally(() => {
      this.#attempts.delete(attempt);
      if (this.#establishing === attempt) this.#establishing = null;
    });
    this.#attempts.add(attempt);
    return attempt;
  }

  #cancel(attempt: EstablishAttempt): void {
    if (attempt.cancelled) return;
    attempt.cancelled = true;
    if (this.#establishing === attempt) this.#establishing = null;
    attempt.process?.kill('SIGTERM');
  }

  async #establish(interactive: boolean, attempt: EstablishAttempt): Promise<void> {
    const checkProcess = this.#ssh(['-O', 'check', '--', this.#options.alias]);
    attempt.process = checkProcess;
    const check = await this.#finish(checkProcess);
    attempt.process = null;
    this.#stillCurrent(attempt);
    if (check.code === 0) return;
    const env: Record<string, string> = interactive
      ? {SSH_ASKPASS: this.#options.askpass, SSH_ASKPASS_REQUIRE: 'force'}
      : {};
    const masterProcess = this.#ssh(
      [
        ...(interactive ? [] : ['-o', 'BatchMode=yes']),
        '-T',
        '-o',
        'ControlMaster=auto',
        '-o',
        'ControlPersist=30m',
        '-o',
        'ConnectTimeout=20',
        '--',
        this.#options.alias,
        'true',
      ],
      undefined,
      env,
    );
    attempt.process = masterProcess;
    const master = await this.#finish(masterProcess);
    attempt.process = null;
    this.#stillCurrent(attempt);
    if (master.code !== 0) throw this.#sshError(master.stderr);
  }

  #stillCurrent(attempt: EstablishAttempt): void {
    if (attempt.cancelled)
      throw new ConnectionError('link', 'the connection attempt was abandoned');
  }

  #ssh(
    args: readonly string[],
    command?: string,
    env: Record<string, string> = {},
  ): SpawnedProcess {
    const common = [
      '-o',
      `ControlPath=${this.#options.controlPath}`,
      '-o',
      'ServerAliveInterval=15',
      '-o',
      'ServerAliveCountMax=3',
    ];
    return this.#spawn(
      'ssh',
      [...common, ...args, ...(command === undefined ? [] : [command])],
      env,
    );
  }

  /** Wait for a short ssh command; its exit status and standard error. */
  async #finish(process: SpawnedProcess): Promise<{code: number | null; stderr: string}> {
    process.stdin.end();
    let stderr = '';
    process.stderr.setEncoding('utf8');
    process.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-4096);
    });
    process.stdout.resume();
    return {code: await process.exit, stderr};
  }

  /** An ssh status 255: the host refused the user or an untrusted host key (`auth`), or `link`. */
  #sshError(stderr: string): ConnectionError {
    const alias = this.#options.alias;
    if (AUTH_FAILURE.test(stderr))
      return new ConnectionError('auth', `ssh ${alias}: ${lastLine(stderr)}`);
    return new ConnectionError('link', `the connection to ${alias} is down: ${lastLine(stderr)}`);
  }

  #releasedError(): ConnectionError {
    return new ConnectionError('link', `the connection to ${this.#options.alias} was released`);
  }
}
