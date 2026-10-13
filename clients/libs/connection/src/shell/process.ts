/**
 * A started process as a link observes it, and the adapter from a real child process.
 *
 * Every process a link starts (a helper channel over `/bin/sh` or `ssh`, an `ssh` control
 * command) goes through this shape, so the connection runs its contract suite against in-memory
 * processes.
 */
import type {ChildProcess} from 'node:child_process';
import type {Readable, Writable} from 'node:stream';
import type {ScheduleTimeout} from '@vibesys/transport';

export interface SpawnedProcess {
  readonly stdin: Writable;
  readonly stdout: Readable;
  readonly stderr: Readable;
  /** True until this adapter observes that process termination has begun. */
  readonly running: boolean;
  /**
   * Settles with the exit code once the process has ended and its output has been read; null when
   * it ended by signal or never started.
   */
  readonly exit: Promise<number | null>;
  kill(signal: 'SIGTERM' | 'SIGKILL'): void;
}

/** What bounded retirement proved about the exact child process. */
type ProcessRetirementOutcome =
  | {readonly kind: 'confirmed'; readonly code: number | null}
  | {readonly kind: 'unconfirmed'};

/**
 * One idempotent retirement of a child. `outcome` settles within two grace periods; `exit` remains
 * the exact termination proof when that bound expires first.
 */
export interface ProcessRetirement {
  readonly outcome: Promise<ProcessRetirementOutcome>;
  readonly exit: Promise<number | null>;
}

/** A retirement whose ownership is published but whose first signal has not necessarily fired. */
export interface PreparedProcessRetirement extends ProcessRetirement {
  readonly start: () => void;
}

interface OwnedProcessRetirement extends PreparedProcessRetirement {
  readonly process: SpawnedProcess;
}

const DEFAULT_TERMINATION_GRACE_MS = 1_000;
const retirements = new WeakMap<SpawnedProcess, OwnedProcessRetirement>();
/** Retain exact process ownership after a bounded outcome reports that termination is unconfirmed. */
const ownedRetirements = new Set<OwnedProcessRetirement>();

/**
 * Terminate `process` once: TERM now, KILL after one grace, and an unconfirmed outcome after a
 * second. Repeated callers join the first operation and its bounds. The shell retains an
 * unconfirmed process until its exact `exit` eventually settles.
 */
export function prepareProcessRetirement(
  process: SpawnedProcess,
  scheduleTimeout: ScheduleTimeout,
  graceMs = DEFAULT_TERMINATION_GRACE_MS,
): PreparedProcessRetirement {
  const existing = retirements.get(process);
  if (existing !== undefined) return existing;
  if (!Number.isSafeInteger(graceMs) || graceMs < 1) {
    throw new RangeError('process termination grace must be a positive integer');
  }

  let cancelKill = (): void => {};
  let cancelConfirmation = (): void => {};
  let settle = (_outcome: ProcessRetirementOutcome): void => {};
  let settled = false;
  const outcome = new Promise<ProcessRetirementOutcome>(resolve => {
    settle = result => {
      if (settled) return;
      settled = true;
      cancelKill();
      cancelConfirmation();
      resolve(result);
    };
  });
  let started = false;
  const start = (): void => {
    if (started || settled) return;
    started = true;
    process.kill('SIGTERM');
    cancelKill = scheduleTimeout(() => {
      process.kill('SIGKILL');
      cancelConfirmation = scheduleTimeout(() => settle({kind: 'unconfirmed'}), graceMs);
    }, graceMs);
  };
  const retirement: OwnedProcessRetirement = {process, outcome, exit: process.exit, start};
  retirements.set(process, retirement);
  ownedRetirements.add(retirement);
  void process.exit.then(
    code => {
      ownedRetirements.delete(retirement);
      settle({kind: 'confirmed', code});
    },
    () => {
      process.kill('SIGKILL');
      settle({kind: 'unconfirmed'});
    },
  );
  return retirement;
}

/** Prepare and immediately start one bounded child retirement. */
export function retireProcess(
  process: SpawnedProcess,
  scheduleTimeout: ScheduleTimeout,
  graceMs = DEFAULT_TERMINATION_GRACE_MS,
): ProcessRetirement {
  const retirement = prepareProcessRetirement(process, scheduleTimeout, graceMs);
  retirement.start();
  return retirement;
}

/** A real child process spawned with three pipes. */
export function fromChild(child: ChildProcess): SpawnedProcess {
  let running = true;
  const exit = new Promise<number | null>(resolve => {
    child.once('error', () => {
      running = false;
      resolve(null);
    });
    child.once('exit', () => {
      running = false;
    });
    child.once('close', code => {
      running = false;
      resolve(code);
    });
  });
  // A process that exits before reading its input makes a later write fail with EPIPE; the exit
  // code reports that outcome, so the write error carries nothing more.
  child.stdin?.on('error', () => {});
  return {
    // `stdio: 'pipe'` guarantees all three streams.
    stdin: child.stdin as Writable,
    stdout: child.stdout as Readable,
    stderr: child.stderr as Readable,
    get running() {
      return running;
    },
    exit,
    kill: signal => {
      if (child.exitCode === null && child.signalCode === null) child.kill(signal);
    },
  };
}
