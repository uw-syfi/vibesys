/**
 * A started process as a link observes it, and the adapter from a real child process.
 *
 * Every process a link starts (a helper channel over `/bin/sh` or `ssh`, an `ssh` control
 * command) goes through this shape, so the connection runs its contract suite against in-memory
 * processes.
 */
import type {ChildProcess} from 'node:child_process';
import type {Readable, Writable} from 'node:stream';

export interface SpawnedProcess {
  readonly stdin: Writable;
  readonly stdout: Readable;
  readonly stderr: Readable;
  /**
   * Settles with the exit code once the process has ended and its output has been read; null when
   * it ended by signal or never started.
   */
  readonly exit: Promise<number | null>;
  kill(signal: 'SIGTERM' | 'SIGKILL'): void;
}

/** A real child process spawned with three pipes. */
export function fromChild(child: ChildProcess): SpawnedProcess {
  const exit = new Promise<number | null>(resolve => {
    child.once('error', () => resolve(null));
    child.once('close', code => resolve(code));
  });
  // A process that exits before reading its input makes a later write fail with EPIPE; the exit
  // code reports that outcome, so the write error carries nothing more.
  child.stdin?.on('error', () => {});
  return {
    // `stdio: 'pipe'` guarantees all three streams.
    stdin: child.stdin as Writable,
    stdout: child.stdout as Readable,
    stderr: child.stderr as Readable,
    exit,
    kill: signal => {
      if (child.exitCode === null && child.signalCode === null) child.kill(signal);
    },
  };
}
