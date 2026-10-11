/**
 * A started process as the hosts observe it, and the adapter from a real child process.
 *
 * Every process a host starts (a local `vibesys` command, an `ssh` client) goes through this shape,
 * so the hosts run their contract suite against in-memory processes.
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

/** What a finished command printed and how it exited. */
export interface CommandOutput {
  readonly code: number | null;
  readonly stdout: string;
  readonly stderr: string;
}

/** A host command crossed the output bound and was killed before it could exhaust Electron. */
export class ProcessOutputLimitError extends Error {
  readonly stream: 'stdout' | 'stderr';
  readonly limitBytes: number;

  constructor(stream: 'stdout' | 'stderr', limitBytes: number) {
    super(`command ${stream} exceeded the ${limitBytes}-byte output limit`);
    this.name = 'ProcessOutputLimitError';
    this.stream = stream;
    this.limitBytes = limitBytes;
  }
}

/**
 * Wait for `process` to finish, retaining at most `limitBytes` from each output stream.
 *
 * Crossing either bound kills the owned child exactly once and rejects after the child and both
 * pipes have ended. Output is never truncated into a superficially valid command result.
 */
export async function finish(process: SpawnedProcess, limitBytes: number): Promise<CommandOutput> {
  process.stdin.end();
  let overflow: ProcessOutputLimitError | null = null;
  const exceeded = (stream: 'stdout' | 'stderr'): void => {
    if (overflow !== null) return;
    overflow = new ProcessOutputLimitError(stream, limitBytes);
    // A command that broke its output contract must not get another chance to keep writing.
    process.kill('SIGKILL');
  };
  const [stdout, stderr, code] = await Promise.all([
    collect(process.stdout, 'stdout', limitBytes, exceeded),
    collect(process.stderr, 'stderr', limitBytes, exceeded),
    process.exit,
  ]);
  if (overflow !== null) throw overflow;
  return {code, stdout, stderr};
}

async function collect(
  stream: Readable,
  name: 'stdout' | 'stderr',
  limitBytes: number,
  exceeded: (stream: 'stdout' | 'stderr') => void,
): Promise<string> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  let retaining = true;
  for await (const value of stream) {
    if (!retaining) continue;
    const chunk =
      typeof value === 'string'
        ? Buffer.from(value)
        : Buffer.isBuffer(value)
          ? value
          : Buffer.from(value as Uint8Array);
    if (bytes + chunk.length > limitBytes) {
      retaining = false;
      chunks.length = 0;
      exceeded(name);
      continue;
    }
    chunks.push(chunk);
    bytes += chunk.length;
  }
  return retaining ? Buffer.concat(chunks, bytes).toString('utf8') : '';
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
