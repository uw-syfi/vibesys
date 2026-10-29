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
  /**
   * Settles once the process itself is dead, without waiting for its output to end. `exit` can
   * stay pending after that: a descendant the process started (ssh's ProxyCommand) may keep an
   * inherited pipe open, and the pipe ends only when that descendant does. Retirement confirms
   * on this, because it asks whether the process is gone, not whether every pipe is.
   */
  readonly terminated: Promise<void>;
  kill(signal: 'SIGTERM' | 'SIGKILL'): void;
}

/** What bounded retirement proved about the exact child process. */
type ProcessRetirementOutcome =
  | {readonly kind: 'confirmed'; readonly code: number | null; readonly pipesHeld: boolean}
  | {readonly kind: 'unconfirmed'};

/**
 * One idempotent retirement of a child. `outcome` settles within two grace periods; `exit` remains
 * the exact termination proof when that bound expires first. A process that `terminated` but whose
 * `exit` stays pending (a descendant holds its pipes) is confirmed, with a null code and
 * `pipesHeld`, one grace after it terminated.
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
  let cancelDrain = (): void => {};
  let settle = (_outcome: ProcessRetirementOutcome): void => {};
  let settled = false;
  const outcome = new Promise<ProcessRetirementOutcome>(resolve => {
    settle = result => {
      if (settled) return;
      settled = true;
      cancelKill();
      cancelConfirmation();
      cancelDrain();
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
  void process.terminated.then(() => {
    // The process is dead; give its output one grace to end and report the exit code, then stop
    // waiting for pipes that a descendant may hold open.
    cancelDrain = scheduleTimeout(() => {
      ownedRetirements.delete(retirement);
      settle({kind: 'confirmed', code: null, pipesHeld: true});
    }, graceMs);
  });
  void process.exit.then(
    code => {
      ownedRetirements.delete(retirement);
      settle({kind: 'confirmed', code, pipesHeld: false});
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
  const alreadyExited = child.exitCode !== null || child.signalCode !== null;
  let running = !alreadyExited;
  let markTerminated = (): void => {};
  const terminated = alreadyExited
    ? Promise.resolve()
    : new Promise<void>(resolve => {
        markTerminated = resolve;
      });
  const exit = alreadyExited
    ? Promise.resolve(child.exitCode)
    : new Promise<number | null>(resolve => {
        // `error` is not an exit event: Node also emits it when `kill()` cannot signal a process
        // that is still live. Only the pre-spawn form proves there is no child whose exit remains
        // to be observed. `pid` covers a successful spawn whose `spawn` event preceded adaptation;
        // the event covers structural implementations that publish the two in the other order.
        let spawned = child.pid !== undefined;
        child.once('spawn', () => {
          spawned = true;
        });
        // Keep observing after a post-spawn error. Retirement can try TERM and then KILL, and each
        // failed signal may emit its own error; removing this listener after the first would turn
        // the second owned failure into an uncaught EventEmitter error.
        child.on('error', () => {
          if (spawned || child.pid !== undefined) return;
          running = false;
          markTerminated();
          resolve(null);
        });
        child.once('exit', () => {
          running = false;
          markTerminated();
        });
        child.once('close', code => {
          running = false;
          markTerminated();
          resolve(code);
        });
      });
  // Keep an absorber across the gap before a helper channel owns this stream, and for short ssh
  // commands that never acquire its acknowledged-input adapter. A helper channel adds its own
  // listener; EventEmitter delivers the error to both, so that owner still reports the write fault.
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
    terminated,
    kill: signal => {
      if (child.exitCode === null && child.signalCode === null) child.kill(signal);
    },
  };
}
