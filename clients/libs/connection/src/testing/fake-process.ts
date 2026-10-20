/**
 * In-memory `SpawnedProcess`es for the hosts' fakes. Like a real child, a process reports its exit
 * only after both outputs have ended, and `kill` ends it with a null code.
 */
import {PassThrough} from 'node:stream';
import type {SpawnedProcess} from '../shell/process.js';

type KillSignal = Parameters<SpawnedProcess['kill']>[0];

export interface FakeProcess {
  readonly process: SpawnedProcess;
  /** What the host wrote to the process's stdin. */
  readonly input: PassThrough;
  readonly stdout: PassThrough;
  readonly stderr: PassThrough;
  /** End the process with `code`, after writing `stdout` and `stderr`. Idempotent. */
  end(code: number | null, stdout?: string, stderr?: string): void;
  /** End the outputs a `pipeHeldProcess` still holds open after its process died. Idempotent. */
  releasePipes(): void;
}

export function fakeProcess(onKill: (signal: KillSignal) => void = () => {}): FakeProcess {
  return buildFakeProcess(onKill, true, false);
}

/**
 * A process that dies on kill while a descendant (ssh's ProxyCommand) still holds its output
 * pipes: `terminated` settles at once, `exit` only after the test calls `releasePipes`.
 */
export function pipeHeldProcess(onKill: (signal: KillSignal) => void = () => {}): FakeProcess {
  return buildFakeProcess(onKill, true, true);
}

/** A process whose kill request is not confirmed until the test explicitly calls `end`. */
export function deferredKillProcess(onKill: (signal: KillSignal) => void = () => {}): FakeProcess {
  return buildFakeProcess(onKill, false, false);
}

function buildFakeProcess(
  onKill: (signal: KillSignal) => void,
  settleOnKill: boolean,
  holdPipes: boolean,
): FakeProcess {
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
  let pendingOutput: [string, string] | null = null;
  const end = (value: number | null, out = '', err = ''): void => {
    if (ended) return;
    ended = true;
    pendingOutput = [out, err];
    if (!holdPipes) releasePipes();
    input.destroy();
    resolveCode(value);
  };
  const releasePipes = (): void => {
    if (pendingOutput === null) return;
    const [out, err] = pendingOutput;
    pendingOutput = null;
    stdout.end(out);
    stderr.end(err);
  };
  return {
    process: {
      stdin: input,
      stdout,
      stderr,
      get running() {
        return !ended;
      },
      exit,
      terminated: code.then(() => {}),
      kill: signal => {
        if (ended) return;
        onKill(signal);
        if (settleOnKill) end(null);
      },
    },
    input,
    stdout,
    stderr,
    end,
    releasePipes,
  };
}
