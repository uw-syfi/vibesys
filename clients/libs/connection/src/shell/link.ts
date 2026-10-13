/**
 * How the app reaches one host: the `Link` role, and its two implementations.
 *
 * A link brings the way to the host up (`establish`) and starts one host helper per channel
 * (`spawnHelper`) from a checkout; everything after that is the helper's typed channel, the same
 * for every host. There is one link per host, whatever checkouts its connections run from: it owns
 * the host's ssh master pool and what the host's bootstraps reported (`heard`).
 *
 * - `LocalLink` (This Mac): no link to establish; each helper is a child process running the
 *   bootstrap with an argument vector.
 * - `SshLink`: the system OpenSSH client, so the user's `~/.ssh/config`, ProxyJump, agent,
 *   certificates, and Kerberos work unchanged. The app owns one master connection per host
 *   (`ControlMaster`/`ControlPath`/`ControlPersist`), and only the master authenticates: it is the
 *   base master is the one ssh started with an askpass helper, so a password or 2FA prompt appears
 *   once, as a dialog, and only when the user asked (`interactive`). Spillover masters are
 *   non-interactive. Each carries `ServerAliveInterval`, so a half-open link ends it instead of
 *   hanging, and `ControlPersist=30m`, so a master an app crash left behind ends on its own. Every
 *   helper is an exec channel over its admitted master with `BatchMode=yes`: it never prompts.
 */
import {spawn} from 'node:child_process';
import {homedir} from 'node:os';
import type {ScheduleTimeout} from '@vibesys/transport';
import {
  type AdmissionConfig,
  type AdmissionOutput,
  type AdmissionState,
  type ChannelRole,
  initialAdmissionState,
  type LinkPoolDiagnostics,
  type MasterRetirementOutcome,
  poolDiagnostics,
  stepAdmission,
} from '../state/channel-admission.js';
import {bootstrapUv, localBootstrap, remoteBootstrap} from './bootstrap.js';
import {ConnectionError} from './helper-channel.js';
import {fromChild, type SpawnedProcess} from './process.js';

/** No eligible master can admit this role within the pool's configured bound. */
export class ChannelCapacityError extends Error {
  readonly role: ChannelRole;

  constructor(role: ChannelRole) {
    super(`the SSH channel pool has no capacity for ${role}`);
    this.name = 'ChannelCapacityError';
    this.role = role;
  }
}

/** The OpenSSH adapter's normalized evidence that one selected master refused a session. */
class SessionCapacityError extends ConnectionError {
  constructor() {
    super('link', 'the selected SSH master refused another session');
    this.name = 'SessionCapacityError';
  }
}

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
  /**
   * Admit and start one role-named helper, then let its owner perform the typed handshake. The link
   * may repeat that unopened handshake once on a spillover master after a normalized per-master
   * capacity refusal. No other failure is retried here.
   */
  withHelper<T>(
    checkout: string,
    role: ChannelRole,
    signal: AbortSignal,
    open: (process: SpawnedProcess) => Promise<T>,
  ): Promise<T>;
  /** A line a bootstrap printed before its helper answered; the link keeps what it reports. */
  heard(line: string): void;
  /** Why a helper's process for `where` (`CHECKOUT on HOST`) ended, when the helper did not say. */
  explain(code: number | null, stderr: string, where: string): ConnectionError;
  /** Bounded resource facts, with no connection strings, paths, commands, or error text. */
  diagnostics(): LinkPoolDiagnostics;
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
  #released = false;
  /** The `uv` a bootstrap found, passed to the next one. */
  #uv: string | null = null;

  constructor(run: Spawn = nodeSpawn) {
    this.#spawn = run;
  }

  async establish(): Promise<void> {}

  networkChanged(): void {}

  async withHelper<T>(
    checkout: string,
    _role: ChannelRole,
    signal: AbortSignal,
    open: (process: SpawnedProcess) => Promise<T>,
  ): Promise<T> {
    if (this.#released) throw new ConnectionError('link', 'the local connection was released');
    if (signal.aborted) throw cancellationReason(signal);
    return open(this.#spawn('/bin/sh', localBootstrap(checkout, this.#uv), {}));
  }

  heard(line: string): void {
    this.#uv = bootstrapUv(line) ?? this.#uv;
  }

  explain(code: number | null, stderr: string, where: string): ConnectionError {
    return helperExit(code, stderr, where);
  }

  diagnostics(): LinkPoolDiagnostics {
    return {
      masterCount: 0,
      opening: emptyChannelCounts(),
      active: emptyChannelCounts(),
      masters: [],
    };
  }

  async release(): Promise<void> {
    this.#released = true;
  }
}

export interface SshLinkOptions {
  /** The `Host` alias from `~/.ssh/config` (validated by the settings). */
  readonly alias: string;
  /** The master connection's socket path on this machine; short (Unix sockets cap paths). */
  readonly controlPath: string;
  /** The askpass helper the master uses for password and 2FA prompts. */
  readonly askpass: string;
  readonly spawn?: Spawn;
  readonly capacityPerMaster?: number;
  readonly reservedControlSlots?: number;
  readonly maxMasters?: number;
  /** Every retirement control command is killed after this bound. */
  readonly retirementCommandTimeoutMs?: number;
  /** Injected timer boundary; production supplies the desktop's real timer implementation. */
  readonly scheduleTimeout: ScheduleTimeout;
}

export const DEFAULT_SSH_POOL_CONFIG: AdmissionConfig = {
  capacityPerMaster: 8,
  reservedControlSlots: 2,
  maxMasters: 4,
};

const DEFAULT_RETIREMENT_COMMAND_TIMEOUT_MS = 1_000;

/** ssh's own exit status for a connection or authentication failure. */
const SSH_FAILED = 255;

/**
 * ssh failures only the user can answer: refused credentials, or a host key that is not trusted yet
 * (BatchMode cannot ask; an interactive sign-in asks through the askpass dialog, never silently).
 */
const AUTH_FAILURE =
  /Permission denied|Too many authentication failures|Authentication failed|Host key verification failed/;

/** OpenSSH's own client-side diagnostic, normalized here before connection policy sees it. */
const SESSION_CAPACITY_FAILURE = /session request failed:\s*Session open refused by peer/i;

interface EstablishAttempt {
  process: SpawnedProcess | null;
  cancelled: boolean;
  readonly waiters: Set<object>;
  promise: Promise<void>;
}

interface AcquiredLease {
  readonly leaseId: number;
  readonly masterId: number;
}

interface AdmissionWaiter {
  readonly role: ChannelRole;
  readonly signal: AbortSignal;
  readonly aborted: () => void;
  readonly resolve: (lease: AcquiredLease) => void;
  readonly reject: (error: Error) => void;
}

/** Shell ownership for one reducer-requested exit and its optional reachability check. */
interface RetirementAttempt {
  process: SpawnedProcess | null;
  cancelDeadline: (() => void) | null;
  promise: Promise<void>;
}

interface RetirementCommandResult {
  /** Whether the shell observed this exact control process terminate. */
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
  #establishing: EstablishAttempt | null = null;
  #released = false;
  #admission: AdmissionState;
  #nextLeaseId = 1;
  /** Async continuations keyed by demand; queue ordering and ownership stay in the pure state. */
  readonly #demandWaiters = new Map<number, AdmissionWaiter>();
  /** Spillover master creations shared by every admission waiting on that master. */
  readonly #openingMasters = new Map<number, Promise<void>>();
  readonly #openingMasterProcesses = new Map<number, SpawnedProcess>();
  /** Every helper exit this link still owns; release joins all of them before masters. */
  readonly #helperExits = new Set<Promise<number | null>>();
  /** Retirements already under way, including the exact command and deadline the shell owns. */
  readonly #retirements = new Map<number, RetirementAttempt>();
  /** Killed control commands that did not confirm exit within their second, bounded grace period. */
  readonly #unconfirmedRetirementCommands = new Set<Promise<number | null>>();
  #releasing: Promise<void> | null = null;
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
    this.#admission = initialAdmissionState({
      capacityPerMaster: options.capacityPerMaster ?? DEFAULT_SSH_POOL_CONFIG.capacityPerMaster,
      reservedControlSlots:
        options.reservedControlSlots ?? DEFAULT_SSH_POOL_CONFIG.reservedControlSlots,
      maxMasters: options.maxMasters ?? DEFAULT_SSH_POOL_CONFIG.maxMasters,
    });
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

  async withHelper<T>(
    checkout: string,
    role: ChannelRole,
    signal: AbortSignal,
    open: (process: SpawnedProcess) => Promise<T>,
  ): Promise<T> {
    const excluded = new Set<number>();
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const lease = await this.#acquire(role, signal, excluded);
      this.#assertLeaseUsable(lease, signal);
      try {
        return await this.#openLeasedHelper(checkout, lease, open);
      } catch (error) {
        if (!(error instanceof SessionCapacityError)) throw error;
        excluded.add(lease.masterId);
        if (attempt === 1) throw new ChannelCapacityError(role);
      }
    }
    throw new ChannelCapacityError(role);
  }

  heard(line: string): void {
    this.#uv = bootstrapUv(line) ?? this.#uv;
  }

  explain(code: number | null, stderr: string, where: string): ConnectionError {
    if (code === SSH_FAILED && SESSION_CAPACITY_FAILURE.test(stderr)) {
      return new SessionCapacityError();
    }
    if (code === SSH_FAILED) return this.#sshError(stderr);
    if (code === null)
      return new ConnectionError('link', `the channel to ${this.#options.alias} broke`);
    return helperExit(code, stderr, where);
  }

  diagnostics(): LinkPoolDiagnostics {
    return poolDiagnostics(this.#admission);
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
    this.#applyAdmission(stepAdmission(this.#admission, {type: 'close'}));
    for (const attempt of this.#attempts) this.#cancel(attempt);
    for (const process of this.#openingMasterProcesses.values()) process.kill('SIGTERM');
    await Promise.all([...this.#attempts].map(attempt => attempt.promise.catch(() => {})));
    await Promise.all([...this.#openingMasters.values()].map(opening => opening.catch(() => {})));
    // The base establishment lifecycle deliberately remains `opening` across a failed attempt so
    // it can be retried. At final close it instead becomes an owned candidate whose absence must be
    // reconciled through the same bounded retirement path.
    this.#markMasterReady(0);
    await Promise.all(this.#helperExits);
    // Lease releases can request new retirements while helpers are draining. Join until the map is
    // empty rather than taking a snapshot that could miss one of those reducer outputs.
    while (this.#retirements.size > 0) {
      await Promise.all([...this.#retirements.values()].map(retirement => retirement.promise));
    }
    if (this.#admission.masters.length > 0 || this.#unconfirmedRetirementCommands.size > 0) {
      const count = this.#admission.masters.length;
      const commands = this.#unconfirmedRetirementCommands.size;
      const detail =
        count > 0
          ? `${count} SSH master${count === 1 ? '' : 's'}`
          : `${commands} SSH control command${commands === 1 ? '' : 's'}`;
      throw new ConnectionError('link', `could not confirm teardown of ${detail}`);
    }
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
    if (check.code === 0) {
      this.#markMasterReady(0);
      return;
    }
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
    this.#markMasterReady(0);
  }

  #stillCurrent(attempt: EstablishAttempt): void {
    if (attempt.cancelled)
      throw new ConnectionError('link', 'the connection attempt was abandoned');
  }

  #ssh(
    args: readonly string[],
    command?: string,
    env: Record<string, string> = {},
    masterId = 0,
  ): SpawnedProcess {
    const common = [
      '-o',
      `ControlPath=${this.#controlPath(masterId)}`,
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

  async #acquire(
    role: ChannelRole,
    signal: AbortSignal,
    excluded: ReadonlySet<number>,
  ): Promise<AcquiredLease> {
    const leaseId = this.#nextLeaseId++;
    if (this.#released) throw this.#releasedError();
    if (signal.aborted) throw cancellationReason(signal);
    if (this.#admission.masters.find(master => master.id === 0)?.phase !== 'ready') {
      throw new ConnectionError('link', 'the SSH link has not been established');
    }
    return new Promise((resolve, reject) => {
      const aborted = (): void => {
        if (!this.#demandWaiters.has(leaseId)) return;
        this.#applyAdmission(stepAdmission(this.#admission, {type: 'cancel', demandId: leaseId}));
      };
      this.#demandWaiters.set(leaseId, {role, signal, aborted, resolve, reject});
      signal.addEventListener('abort', aborted, {once: true});
      this.#applyAdmission(
        stepAdmission(this.#admission, {
          type: 'request',
          demandId: leaseId,
          role,
          excludedMasters: [...excluded],
        }),
      );
    });
  }

  #assertLeaseUsable(lease: AcquiredLease, signal: AbortSignal): void {
    if (!this.#released && !signal.aborted) return;
    this.#releaseLeaseIfOwned(lease.leaseId, lease.masterId);
    if (this.#released) throw this.#releasedError();
    throw cancellationReason(signal);
  }

  async #openLeasedHelper<T>(
    checkout: string,
    lease: AcquiredLease,
    open: (process: SpawnedProcess) => Promise<T>,
  ): Promise<T> {
    const process = this.#ssh(
      ['-T', '-o', 'ControlMaster=no', '-o', 'BatchMode=yes', '--', this.#options.alias],
      remoteBootstrap(checkout, this.#uv),
      {},
      lease.masterId,
    );
    this.#helperExits.add(process.exit);
    let released = false;
    void process.exit.then(() => {
      this.#helperExits.delete(process.exit);
      if (released) return;
      released = true;
      this.#applyAdmission(
        stepAdmission(this.#admission, {type: 'release', leaseId: lease.leaseId}),
      );
    });
    try {
      const result = await open(process);
      if (released) throw new ConnectionError('link', 'the SSH helper ended while opening');
      this.#applyAdmission(
        stepAdmission(this.#admission, {type: 'activate', leaseId: lease.leaseId}),
      );
      return result;
    } catch (error) {
      if (!released) {
        process.kill('SIGTERM');
        await process.exit;
      }
      throw error;
    }
  }

  async #createMasterProcess(masterId: number): Promise<void> {
    const process = this.#ssh(
      [
        '-o',
        'BatchMode=yes',
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
      {},
      masterId,
    );
    this.#openingMasterProcesses.set(masterId, process);
    const result = await this.#finish(process);
    if (this.#released) throw this.#releasedError();
    if (result.code !== 0) throw this.#sshError(result.stderr);
  }

  #releaseLeaseIfOwned(leaseId: number, masterId: number): void {
    const master = this.#admission.masters.find(candidate => candidate.id === masterId);
    if (master?.leases.some(lease => lease.id === leaseId) !== true) return;
    this.#applyAdmission(stepAdmission(this.#admission, {type: 'release', leaseId}));
  }

  #applyAdmission(transition: {
    readonly state: AdmissionState;
    readonly outputs: readonly AdmissionOutput[];
  }): void {
    this.#admission = transition.state;
    for (const output of transition.outputs) {
      switch (output.type) {
        case 'granted':
          this.#settleDemand(output.demandId, waiter =>
            waiter.resolve({leaseId: output.demandId, masterId: output.masterId}),
          );
          break;
        case 'capacity':
          this.#settleDemand(output.demandId, waiter =>
            waiter.reject(new ChannelCapacityError(waiter.role)),
          );
          break;
        case 'cancelled':
          this.#settleDemand(output.demandId, waiter =>
            waiter.reject(cancellationReason(waiter.signal)),
          );
          break;
        case 'closed':
          this.#settleDemand(output.demandId, waiter => waiter.reject(this.#releasedError()));
          break;
        case 'start-master':
          this.#startMaster(output.masterId);
          break;
        case 'stop-master':
          this.#startRetirement(output.masterId);
          break;
      }
    }
  }

  #settleDemand(demandId: number, settle: (waiter: AdmissionWaiter) => void): void {
    const waiter = this.#demandWaiters.get(demandId);
    if (waiter === undefined) return;
    this.#demandWaiters.delete(demandId);
    waiter.signal.removeEventListener('abort', waiter.aborted);
    settle(waiter);
  }

  #startMaster(masterId: number): void {
    if (this.#openingMasters.has(masterId)) {
      throw new Error(`SSH master ${masterId} is already opening`);
    }
    const opening = this.#createMasterProcess(masterId)
      .then(
        () => {
          this.#applyAdmission(stepAdmission(this.#admission, {type: 'master-ready', masterId}));
        },
        () => {
          this.#applyAdmission(stepAdmission(this.#admission, {type: 'master-failed', masterId}));
        },
      )
      .finally(() => {
        this.#openingMasters.delete(masterId);
        this.#openingMasterProcesses.delete(masterId);
      });
    this.#openingMasters.set(masterId, opening);
  }

  #startRetirement(masterId: number): void {
    if (this.#retirements.has(masterId)) return;
    const retirement: RetirementAttempt = {
      process: null,
      cancelDeadline: null,
      promise: Promise.resolve(),
    };
    this.#retirements.set(masterId, retirement);
    retirement.promise = this.#retireMaster(masterId, retirement)
      .then(outcome => {
        this.#applyAdmission(
          stepAdmission(this.#admission, {
            type: 'master-retirement-observed',
            masterId,
            outcome,
          }),
        );
      })
      .finally(() => {
        retirement.cancelDeadline?.();
        retirement.cancelDeadline = null;
        retirement.process = null;
        this.#retirements.delete(masterId);
      });
  }

  async #retireMaster(
    masterId: number,
    retirement: RetirementAttempt,
  ): Promise<MasterRetirementOutcome> {
    const exit = await this.#runRetirementCommand(
      ['-O', 'exit', '--', this.#options.alias],
      masterId,
      retirement,
    );
    if (!exit.confirmed) return 'unknown';
    if (exit.code === 0) return 'retired';
    const check = await this.#runRetirementCommand(
      ['-O', 'check', '--', this.#options.alias],
      masterId,
      retirement,
    );
    if (!check.confirmed || check.code === null) return 'unknown';
    if (check.code === 0) return 'reachable';
    return 'retired';
  }

  async #runRetirementCommand(
    args: readonly string[],
    masterId: number,
    retirement: RetirementAttempt,
  ): Promise<RetirementCommandResult> {
    let process: SpawnedProcess;
    try {
      process = this.#ssh(args, undefined, {}, masterId);
    } catch {
      return {confirmed: false, code: null};
    }
    retirement.process = process;
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
        retirement.cancelDeadline = null;
        if (retirement.process === process) retirement.process = null;
        resolve(result);
      };
      const ownUnconfirmedExit = (): void => {
        const exit = process.exit;
        this.#unconfirmedRetirementCommands.add(exit);
        void exit.then(
          () => this.#unconfirmedRetirementCommands.delete(exit),
          () => this.#unconfirmedRetirementCommands.delete(exit),
        );
        settle({confirmed: false, code: null});
      };
      cancelKill = this.#options.scheduleTimeout(() => {
        process.kill('SIGKILL');
        cancelGrace = this.#options.scheduleTimeout(
          ownUnconfirmedExit,
          this.#retirementCommandTimeoutMs,
        );
        retirement.cancelDeadline = () => {
          cancelKill();
          cancelGrace();
        };
      }, this.#retirementCommandTimeoutMs);
      retirement.cancelDeadline = () => {
        cancelKill();
        cancelGrace();
      };
      void finished.then(
        result => settle({confirmed: true, code: result.code}),
        () => {
          process.kill('SIGKILL');
          ownUnconfirmedExit();
        },
      );
    });
  }

  #markMasterReady(masterId: number): void {
    const master = this.#admission.masters.find(candidate => candidate.id === masterId);
    if (master?.phase === 'opening') {
      this.#applyAdmission(stepAdmission(this.#admission, {type: 'master-ready', masterId}));
    }
  }

  #controlPath(masterId: number): string {
    return masterId === 0 ? this.#options.controlPath : `${this.#options.controlPath}.${masterId}`;
  }

  #releasedError(): ConnectionError {
    return new ConnectionError('link', `the connection to ${this.#options.alias} was released`);
  }
}

function cancellationReason(signal: AbortSignal): Error {
  if (signal.reason instanceof Error) return signal.reason;
  const error = new Error('the channel admission was abandoned');
  error.name = 'AbortError';
  return error;
}

function emptyChannelCounts(): LinkPoolDiagnostics['opening'] {
  return {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0};
}
