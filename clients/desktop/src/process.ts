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

/** Wait for `process` to finish, collecting both its outputs. */
export async function finish(process: SpawnedProcess): Promise<CommandOutput> {
  process.stdin.end();
  const [stdout, stderr, code] = await Promise.all([
    collect(process.stdout),
    collect(process.stderr),
    process.exit,
  ]);
  return {code, stdout, stderr};
}

async function collect(stream: Readable): Promise<string> {
  stream.setEncoding('utf8');
  let text = '';
  for await (const chunk of stream) text += chunk as string;
  return text;
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
