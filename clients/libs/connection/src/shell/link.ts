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
import type {ScheduleTimeout} from '@vibesys/transport';
import {bootstrapUv, localBootstrap, remoteBootstrap, remoteCommand} from './bootstrap.js';
import {ConnectionError} from './helper-channel.js';
import {
  fromChild,
  type ProcessRetirement,
  prepareProcessRetirement,
  type SpawnedProcess,
} from './process.js';

export interface Link {
  /** The host, for messages: `This Mac`, or its ssh alias. */
  readonly host: string;
  /**
   * Bring the link up; only an `interactive` attempt (the user's own action) may prompt. Rejects
   * with a `ConnectionError`: `auth` when the host refused the user, `link` when it is unreachable.
   * Concurrent callers share an attempt while their live intent permits it: any live interactive
   * caller authorizes the master to prompt, but a caller that leaves before launch does not. An
   * interactive caller supersedes a noninteractive master already being started. Aborting one
   * caller stops waiting for it; the attempt is stopped only when it has no callers left.
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
  /** Every master control command is killed after this bound. */
  readonly retirementCommandTimeoutMs?: number;
  /** Injected timer boundary; production supplies the desktop's real timer implementation. */
  readonly scheduleTimeout: ScheduleTimeout;
}

const DEFAULT_RETIREMENT_COMMAND_TIMEOUT_MS = 1_000;

/** ssh's own exit status for a connection or authentication failure. */
const SSH_FAILED = 255;

/**
 * ssh failures only the user can answer: refused credentials, or a host key that is not trusted yet
 * (BatchMode cannot ask; an interactive sign-in asks through the askpass dialog, never silently).
 */
const AUTH_FAILURE =
  /Permission denied|Too many authentication failures|Authentication failed|Host key verification failed/;

/**
 * The host refused a new channel because this app's other channels to it already spent its session
 * ceiling (sshd's `MaxSessions`, default 10: one control channel plus up to three per attached run,
 * `docs/contributing/desktop-connection.md`). `mux_client_request_session: ... Session open refused
 * by peer` is the OpenSSH mux client's own wording for exactly this refusal over the shared
 * `ControlMaster`; `administratively prohibited` is the server-side channel-open-failure reason the
 * same refusal can surface as. Neither is a link problem a retry fixes: the link and the host are
 * both fine, only this app's own channel count against this one host is not.
 */
const SESSION_CAPACITY_REFUSED = /session request failed|administratively prohibited/i;

interface EstablishWaiter {
  readonly interactive: boolean;
}

type EstablishStage = 'checking' | 'background-master' | 'interactive-master';

interface EstablishPromotion {
  requested: boolean;
  readonly promise: Promise<void>;
  readonly request: () => void;
}

interface EstablishAttempt {
  process: SpawnedProcess | null;
  cancelled: boolean;
  stage: EstablishStage;
  promotion: EstablishPromotion | null;
  readonly cancellation: Promise<void>;
  readonly cancel: () => void;
  retirement: ProcessRetirement | null;
  readonly waiters: Set<EstablishWaiter>;
  readonly promise: Promise<void>;
}

interface RetirementCommandResult {
  /** Whether this exact control process was observed to terminate. */
  readonly confirmed: boolean;
  readonly code: number | null;
}

export class SshLink implements Link {
  readonly host: string;
  readonly #options: SshLinkOptions;
  readonly #spawn: Spawn;
  readonly #retirementCommandTimeoutMs: number;
  /** Every establishment subprocess that has not exited, including retired generations. */
  readonly #attempts = new Set<EstablishAttempt>();
  /** Cancelled establishment children whose exact exit has not been observed yet. */
  readonly #retiringAttempts = new Set<ProcessRetirement>();
  /** A synchronous spawn call in progress; reentrant replacements wait until it owns its child. */
  #launchingProcess: Promise<void> | null = null;
  #establishing: EstablishAttempt | null = null;
  #released = false;
  #releasing: Promise<void> | null = null;
  /** Killed control commands that did not confirm exit within their bounded grace period. */
  readonly #unconfirmedRetirementCommands = new Set<Promise<number | null>>();
  /** The `uv` a bootstrap on the host found, passed to the next one. */
  #uv: string | null = null;

  constructor(options: SshLinkOptions) {
    this.#options = options;
    this.#spawn = options.spawn ?? nodeSpawn;
    this.#retirementCommandTimeoutMs =
      options.retirementCommandTimeoutMs ?? DEFAULT_RETIREMENT_COMMAND_TIMEOUT_MS;
    if (
      !Number.isSafeInteger(this.#retirementCommandTimeoutMs) ||
      this.#retirementCommandTimeoutMs < 1
    ) {
      throw new RangeError('retirementCommandTimeoutMs must be a positive integer');
    }
    this.host = options.alias;
  }

  /** Establish the master if it is down; one attempt at a time. */
  establish(interactive: boolean, signal?: AbortSignal): Promise<void> {
    if (this.#released) return Promise.reject(this.#releasedError());
    if (signal?.aborted === true) {
      return Promise.reject(new ConnectionError('link', 'the connection attempt was abandoned'));
    }
    const waiter: EstablishWaiter = {interactive};
    const attempt = this.#admit(waiter);
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

  /** Join the live generation, asking its batch master to retire when this caller may prompt. */
  #admit(waiter: EstablishWaiter): EstablishAttempt {
    const attempt = this.#establishing;
    if (attempt === null) return this.#begin(waiter);
    attempt.waiters.add(waiter);
    if (waiter.interactive && attempt.stage === 'background-master') {
      this.#promote(attempt);
    }
    return attempt;
  }

  #promote(attempt: EstablishAttempt): void {
    const promotion = attempt.promotion;
    if (promotion === null || promotion.requested) return;
    promotion.requested = true;
    this.#retireAttemptProcess(attempt);
    promotion.request();
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

  /** A binary channel over this link's existing SSH master and host configuration. */
  spawnCommand(command: string): SpawnedProcess {
    return this.#ssh(
      ['-T', '-o', 'ControlMaster=no', '-o', 'BatchMode=yes', '--', this.#options.alias],
      remoteCommand(command),
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

  release(): Promise<void> {
    this.#releasing ??= this.#release();
    return this.#releasing;
  }

  async #release(): Promise<void> {
    // Every active or retired attempt owns the same master this release closes. Kill each attempt
    // so quitting is not held hostage by ConnectTimeout, then wait for confirmed exits before
    // `-O exit`; otherwise a retired process could still fork a master after exit completed.
    this.#released = true;
    for (const attempt of this.#attempts) this.#cancel(attempt);
    await Promise.all([...this.#attempts].map(attempt => attempt.promise.catch(() => {})));
    await this.#confirmRetiredAttempts();
    await this.#retireMaster();
  }

  async #confirmRetiredAttempts(): Promise<void> {
    // `spawn` is synchronous but is an injected boundary and may re-enter this link. A cancelled
    // generation publishes this launch barrier before calling it; once it settles, the returned
    // child has either been retired below or the spawn failed without creating one.
    while (this.#launchingProcess !== null) await this.#launchingProcess;
    const outcomes = await Promise.all(
      [...this.#retiringAttempts].map(retirement => retirement.outcome),
    );
    if (outcomes.some(outcome => outcome.kind === 'unconfirmed')) {
      throw this.#unconfirmedAttemptError();
    }
  }

  async #retireMaster(): Promise<void> {
    const exit = await this.#runRetirementCommand(['-O', 'exit', '--', this.#options.alias]);
    if (!exit.confirmed) throw this.#unconfirmedTeardownError();
    if (exit.code === 0) return;
    const check = await this.#runRetirementCommand(['-O', 'check', '--', this.#options.alias]);
    if (!check.confirmed || check.code === null) throw this.#unconfirmedTeardownError();
    if (check.code === 0) {
      throw new ConnectionError('link', 'the SSH master remained reachable after teardown');
    }
  }

  #runRetirementCommand(args: readonly string[]): Promise<RetirementCommandResult> {
    let process: SpawnedProcess;
    try {
      process = this.#ssh(args);
    } catch {
      return Promise.resolve({confirmed: false, code: null});
    }
    const finished = this.#finish(process);
    return new Promise(resolve => {
      let settled = false;
      let cancelKill = (): void => {};
      let cancelGrace = (): void => {};
      const settle = (result: RetirementCommandResult): void => {
        if (settled) return;
        settled = true;
        cancelKill();
        cancelGrace();
        resolve(result);
      };
      const ownUnconfirmedExit = (): void => {
        const commandExit = process.exit;
        this.#unconfirmedRetirementCommands.add(commandExit);
        void commandExit.then(
          () => this.#unconfirmedRetirementCommands.delete(commandExit),
          () => this.#unconfirmedRetirementCommands.delete(commandExit),
        );
        settle({confirmed: false, code: null});
      };
      cancelKill = this.#options.scheduleTimeout(() => {
        process.kill('SIGKILL');
        cancelGrace = this.#options.scheduleTimeout(
          ownUnconfirmedExit,
          this.#retirementCommandTimeoutMs,
        );
      }, this.#retirementCommandTimeoutMs);
      void finished.then(
        result => settle({confirmed: true, code: result.code}),
        () => {
          process.kill('SIGKILL');
          ownUnconfirmedExit();
        },
      );
    });
  }

  #unconfirmedTeardownError(): ConnectionError {
    return new ConnectionError('link', 'could not confirm teardown of the SSH master');
  }

  #unconfirmedAttemptError(): ConnectionError {
    return new ConnectionError(
      'link',
      'could not confirm termination of an SSH connection attempt',
    );
  }

  #begin(waiter: EstablishWaiter): EstablishAttempt {
    let cancel = (): void => {};
    const cancellation = new Promise<void>(resolve => {
      cancel = resolve;
    });
    let resolveAttempt = (): void => {};
    let rejectAttempt = (_error: unknown): void => {};
    const promise = new Promise<void>((resolve, reject) => {
      resolveAttempt = resolve;
      rejectAttempt = reject;
    });
    const attempt: EstablishAttempt = {
      process: null,
      cancelled: false,
      stage: 'checking',
      promotion: null,
      cancellation,
      cancel,
      retirement: null,
      waiters: new Set([waiter]),
      promise,
    };
    // Publish both the logical generation and its stable completion promise before starting it:
    // injected spawn implementations may synchronously re-enter release from the very first check.
    this.#establishing = attempt;
    this.#attempts.add(attempt);
    void this.#establish(attempt).then(resolveAttempt, rejectAttempt);
    void attempt.promise.then(
      () => this.#finishEstablish(attempt),
      () => this.#finishEstablish(attempt),
    );
    return attempt;
  }

  #finishEstablish(attempt: EstablishAttempt): void {
    this.#attempts.delete(attempt);
    if (this.#establishing === attempt) this.#establishing = null;
  }

  #cancel(attempt: EstablishAttempt): void {
    if (attempt.cancelled) return;
    attempt.cancelled = true;
    if (this.#establishing === attempt) this.#establishing = null;
    this.#retireAttemptProcess(attempt);
    attempt.cancel();
  }

  #retireAttemptProcess(attempt: EstablishAttempt): void {
    if (attempt.process === null || attempt.retirement !== null) return;
    const retirement = prepareProcessRetirement(
      attempt.process,
      this.#options.scheduleTimeout,
      this.#retirementCommandTimeoutMs,
    );
    attempt.retirement = retirement;
    this.#retiringAttempts.add(retirement);
    void retirement.exit.then(
      () => this.#retiringAttempts.delete(retirement),
      () => {},
    );
    retirement.start();
  }

  async #establish(attempt: EstablishAttempt): Promise<void> {
    while (await this.#establishOnce(attempt)) {}
  }

  /** Run one check-and-master generation; true asks the same waiters to try a promoted successor. */
  async #establishOnce(attempt: EstablishAttempt): Promise<boolean> {
    if (this.#launchingProcess !== null || this.#retiringAttempts.size > 0) {
      await this.#confirmRetiredAttempts();
    }
    this.#stillCurrent(attempt);
    attempt.stage = 'checking';
    const checkProcess = this.#launchAttempt(attempt, ['-O', 'check', '--', this.#options.alias]);
    const check = await this.#finishAttempt(checkProcess, attempt);
    attempt.process = null;
    this.#stillCurrent(attempt);
    if (check.code === 0) return false;
    const interactive = [...attempt.waiters].some(waiter => waiter.interactive);
    const promotion = interactive ? null : this.#promotion();
    attempt.promotion = promotion;
    attempt.stage = interactive ? 'interactive-master' : 'background-master';
    const env: Record<string, string> = interactive
      ? {SSH_ASKPASS: this.#options.askpass, SSH_ASKPASS_REQUIRE: 'force'}
      : {};
    const masterProcess = this.#launchAttempt(
      attempt,
      [
        ...(interactive ? [] : ['-o', 'BatchMode=yes']),
        '-T',
        '-o',
        'ControlMaster=auto',
        '-o',
        'ControlPersist=30m',
        '-o',
        'ConnectTimeout=20',
        // Every helper channel is a session on this master, so this compresses all of them. A
        // run's journal is text that shrinks 8-20x; an 8 MB history reads in less than half the time.
        '-o',
        'Compression=yes',
        '--',
        this.#options.alias,
        'true',
      ],
      env,
    );
    const master = await this.#finishMaster(masterProcess, attempt, promotion);
    attempt.process = null;
    attempt.promotion = null;
    this.#stillCurrent(attempt);
    if (master.kind === 'promoted') {
      attempt.retirement = null;
      return true;
    }
    if (master.code !== 0) throw this.#sshError(master.stderr);
    return false;
  }

  #promotion(): EstablishPromotion {
    let request = (): void => {};
    const promise = new Promise<void>(resolve => {
      request = resolve;
    });
    return {requested: false, promise, request};
  }

  /** Publish synchronous launch ownership before crossing the injected process boundary. */
  #launchAttempt(
    attempt: EstablishAttempt,
    args: readonly string[],
    env: Record<string, string> = {},
  ): SpawnedProcess {
    let finishLaunch = (): void => {};
    const launching = new Promise<void>(resolve => {
      finishLaunch = resolve;
    });
    this.#launchingProcess = launching;
    try {
      const process = this.#ssh(args, undefined, env);
      attempt.process = process;
      if (attempt.cancelled || attempt.promotion?.requested === true) {
        this.#retireAttemptProcess(attempt);
      }
      return process;
    } finally {
      if (this.#launchingProcess === launching) this.#launchingProcess = null;
      finishLaunch();
    }
  }

  #stillCurrent(attempt: EstablishAttempt): void {
    if (attempt.cancelled)
      throw new ConnectionError('link', 'the connection attempt was abandoned');
  }

  #finishAttempt(
    process: SpawnedProcess,
    attempt: EstablishAttempt,
  ): Promise<{code: number | null; stderr: string}> {
    return Promise.race([this.#finish(process), this.#cancelled(attempt)]);
  }

  #finishMaster(
    process: SpawnedProcess,
    attempt: EstablishAttempt,
    promotion: EstablishPromotion | null,
  ): Promise<
    | {readonly kind: 'finished'; readonly code: number | null; readonly stderr: string}
    | {readonly kind: 'promoted'}
  > {
    const finished = this.#finish(process).then(async result => {
      if (promotion?.requested === true) return this.#confirmedPromotion(attempt);
      return {kind: 'finished' as const, ...result};
    });
    if (promotion === null) return Promise.race([finished, this.#cancelled(attempt)]);
    const promoted = promotion.promise.then(() => this.#confirmedPromotion(attempt));
    return Promise.race([finished, promoted, this.#cancelled(attempt)]);
  }

  async #confirmedPromotion(attempt: EstablishAttempt): Promise<{readonly kind: 'promoted'}> {
    const retirement = attempt.retirement;
    if (retirement === null) {
      throw new Error('a promoted SSH attempt did not own its batch process retirement');
    }
    const outcome = await retirement.outcome;
    if (outcome.kind === 'unconfirmed') throw this.#unconfirmedAttemptError();
    return {kind: 'promoted'};
  }

  #cancelled(attempt: EstablishAttempt): Promise<never> {
    return attempt.cancellation.then(async () => {
      const retirement = attempt.retirement;
      if (retirement !== null) {
        const outcome = await retirement.outcome;
        if (outcome.kind === 'unconfirmed') throw this.#unconfirmedAttemptError();
      }
      throw new ConnectionError('link', 'the connection attempt was abandoned');
    });
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

  /**
   * An ssh status 255: the host refused the user or an untrusted host key (`auth`), this app's own
   * channel count against the host spent its session ceiling (`capacity`), or `link`.
   */
  #sshError(stderr: string): ConnectionError {
    const alias = this.#options.alias;
    if (SESSION_CAPACITY_REFUSED.test(stderr)) {
      return new ConnectionError(
        'capacity',
        `too many runs are already open against ${alias}; close one or wait, then retry (${lastLine(stderr)})`,
      );
    }
    if (AUTH_FAILURE.test(stderr))
      return new ConnectionError('auth', `ssh ${alias}: ${lastLine(stderr)}`);
    return new ConnectionError('link', `the connection to ${alias} is down: ${lastLine(stderr)}`);
  }

  #releasedError(): ConnectionError {
    return new ConnectionError('link', `the connection to ${this.#options.alias} was released`);
  }
}
