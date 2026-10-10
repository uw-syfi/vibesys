/**
 * In-memory `SpawnedProcess`es for the hosts' fakes. Like a real child, a process reports its exit
 * only after both outputs have ended, and `kill` ends it with a null code.
 */
import {PassThrough} from 'node:stream';
import type {SpawnedProcess} from '../process.js';

export interface FakeProcess {
  readonly process: SpawnedProcess;
  /** What the host wrote to the process's stdin. */
  readonly input: PassThrough;
  readonly stdout: PassThrough;
  readonly stderr: PassThrough;
  /** End the process with `code`, after writing `stdout` and `stderr`. Idempotent. */
  end(code: number | null, stdout?: string, stderr?: string): void;
}

export function fakeProcess(onKill: () => void = () => {}): FakeProcess {
  const input = new PassThrough();
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  let resolveCode: (code: number | null) => void = () => {};
  const code = new Promise<number | null>(resolve => {
    resolveCode = resolve;
  });
  const drained = (stream: PassThrough): Promise<void> =>
    new Promise(resolve => {
      if (stream.readableEnded) resolve();
      else stream.once('end', () => resolve());
    });
  const exit = Promise.all([drained(stdout), drained(stderr), code]).then(([, , value]) => value);
  let ended = false;
  const end = (value: number | null, out = '', err = ''): void => {
    if (ended) return;
    ended = true;
    stdout.end(out);
    stderr.end(err);
    input.destroy();
    resolveCode(value);
  };
  return {
    process: {
      stdin: input,
      stdout,
      stderr,
      exit,
      kill: () => {
        if (ended) return;
        onKill();
        end(null);
      },
    },
    input,
    stdout,
    stderr,
    end,
  };
}

/** A process that has already printed `stdout` and `stderr` and exited with `code`. */
export function finishedProcess(code: number | null, stdout = '', stderr = ''): SpawnedProcess {
  const fake = fakeProcess();
  fake.end(code, stdout, stderr);
  return fake.process;
}
