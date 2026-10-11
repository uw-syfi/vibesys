/**
 * How the app reaches one host: the `Link` role, and its two implementations.
 *
 * A link brings the way to the host up (`establish`) and starts one host helper per channel
 * (`spawnHelper`); everything after that is the helper's typed channel, the same for every host.
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
import {localBootstrap, remoteBootstrap} from './bootstrap.js';
import {ConnectionError} from './helper-channel.js';
import {fromChild, type SpawnedProcess} from './process.js';

export interface Link {
  /** `CHECKOUT on HOST`, for messages. */
  readonly where: string;
  /**
   * Bring the link up; only an `interactive` attempt (the user's own action) may prompt. Rejects
   * with a `ConnectionError`: `auth` when the host refused the user, `link` when it is unreachable.
   */
  establish(interactive: boolean): Promise<void>;
  /** Start one helper on the host: a new channel. */
  spawnHelper(): SpawnedProcess;
  /** Why a helper's process ended, when the helper did not say. */
  explain(code: number | null, stderr: string): ConnectionError;
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
  readonly where: string;
  readonly #checkout: string;
  readonly #spawn: Spawn;

  constructor(checkout: string, run: Spawn = nodeSpawn) {
    this.#checkout = checkout;
    this.#spawn = run;
    this.where = `${checkout} on This Mac`;
  }

  async establish(): Promise<void> {}

  spawnHelper(): SpawnedProcess {
    return this.#spawn('/bin/sh', localBootstrap(this.#checkout), {});
  }

  explain(code: number | null, stderr: string): ConnectionError {
    return helperExit(code, stderr, this.where);
  }

  async release(): Promise<void> {}
}

export interface SshLinkOptions {
  /** The `Host` alias from `~/.ssh/config` (validated by the settings). */
  readonly alias: string;
  /** The VibeSys checkout on the host (validated by the settings), e.g. `~/src/vibesys`. */
  readonly checkout: string;
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

export class SshLink implements Link {
  readonly where: string;
  readonly #options: SshLinkOptions;
  readonly #spawn: Spawn;
  #establishing: Promise<void> | null = null;

  constructor(options: SshLinkOptions) {
    this.#options = options;
    this.#spawn = options.spawn ?? nodeSpawn;
    this.where = `${options.checkout} on ${options.alias}`;
  }

  /** Establish the master if it is down; one attempt at a time. */
  establish(interactive: boolean): Promise<void> {
    this.#establishing ??= this.#establish(interactive).finally(() => {
      this.#establishing = null;
    });
    return this.#establishing;
  }

  spawnHelper(): SpawnedProcess {
    return this.#ssh(
      ['-T', '-o', 'ControlMaster=no', '-o', 'BatchMode=yes', '--', this.#options.alias],
      remoteBootstrap(this.#options.checkout),
    );
  }

  explain(code: number | null, stderr: string): ConnectionError {
    if (code === SSH_FAILED) return this.#sshError(stderr);
    if (code === null)
      return new ConnectionError('link', `the channel to ${this.#options.alias} broke`);
    return helperExit(code, stderr, this.where);
  }

  async release(): Promise<void> {
    await this.#finish(this.#ssh(['-O', 'exit', '--', this.#options.alias]));
  }

  async #establish(interactive: boolean): Promise<void> {
    const check = await this.#finish(this.#ssh(['-O', 'check', '--', this.#options.alias]));
    if (check.code === 0) return;
    const env: Record<string, string> = interactive
      ? {SSH_ASKPASS: this.#options.askpass, SSH_ASKPASS_REQUIRE: 'force'}
      : {};
    const master = await this.#finish(
      this.#ssh(
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
      ),
    );
    if (master.code !== 0) throw this.#sshError(master.stderr);
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
}
