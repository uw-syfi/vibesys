/**
 * In-memory processes and a `ProcessSpawner` over them. Like a real child, a process reports its
 * exit only after both outputs have ended, and a kill ends it with a null code unless the test
 * made it stubborn.
 */
import {PassThrough} from 'node:stream';
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Layer from 'effect/Layer';
import {SpawnFailed} from '../../main/errors.js';
import {type KillSignal, ProcessSpawner, type SpawnedProcess} from '../../main/process.js';
import {invokeCallback} from '../callback-errors.js';

/** How a fake process answers a kill. */
export type KillBehavior =
  /** Ends at once with a null code. */
  | 'dies'
  /** Dies, but a descendant keeps its output open until `releasePipes`. */
  | 'dies-holding-pipes'
  /** Ignores every signal until the test ends it. */
  | 'ignores';

export interface FakeProcess {
  readonly process: SpawnedProcess;
  /** What the code under test wrote to the process's stdin. */
  readonly input: PassThrough;
  readonly stdout: PassThrough;
  readonly stderr: PassThrough;
  readonly signals: KillSignal[];
  /** End the process with `code`, after writing `stdout` and `stderr`. Idempotent. */
  end(code: number | null, stdout?: string, stderr?: string): void;
  /** End the outputs a `dies-holding-pipes` process still holds open. Idempotent. */
  releasePipes(): void;
}

export function fakeProcess(kill: KillBehavior = 'dies'): FakeProcess {
  // Keep backpressure deterministic across Node versions (whose default buffer size differs).
  const input = new PassThrough({highWaterMark: 16 * 1024});
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  const signals: KillSignal[] = [];
  const code = Deferred.makeUnsafe<number | null>();
  const terminated = Deferred.makeUnsafe<void>();
  const drainedBoth = Deferred.makeUnsafe<void>();
  const drained = (stream: PassThrough): Promise<void> =>
    new Promise(resolve => {
      if (stream.writableFinished || stream.destroyed) resolve();
      else {
        // A destroyed pipe never finishes, but it is as ended as a pipe can be.
        stream.once('finish', () => resolve());
        stream.once('close', () => resolve());
      }
    });
  let ended = false;
  let held: [string, string] | null = null;
  let holdPipes = false;
  const releasePipes = (): void => {
    if (held === null) return;
    const [out, err] = held;
    held = null;
    stdout.end(out);
    stderr.end(err);
  };
  const end = (value: number | null, out = '', err = ''): void => {
    if (ended) return;
    ended = true;
    held = [out, err];
    if (!holdPipes) releasePipes();
    input.destroy();
    Deferred.doneUnsafe(code, Exit.succeed(value));
    Deferred.doneUnsafe(terminated, Exit.void);
  };
  void Promise.all([drained(stdout), drained(stderr)]).then(() => {
    Deferred.doneUnsafe(drainedBoth, Exit.void);
  });
  // The reported exit is the code, once the outputs have ended.
  const exit = Effect.gen(function* () {
    yield* Deferred.await(drainedBoth);
    return yield* Deferred.await(code);
  });
  return {
    process: {
      stdin: input,
      stdout,
      stderr,
      exit,
      terminated: Deferred.await(terminated),
      kill: signal =>
        Effect.sync(() => {
          if (ended) return;
          signals.push(signal);
          if (kill === 'ignores') return;
          holdPipes = kill === 'dies-holding-pipes';
          end(null);
        }),
    },
    input,
    stdout,
    stderr,
    signals,
    end,
    releasePipes,
  };
}

/** One start of a process the spawner was asked for. */
export interface Started {
  readonly command: string;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
  readonly process: FakeProcess;
}

export interface FakeSpawner {
  readonly layer: Layer.Layer<ProcessSpawner>;
  readonly started: Started[];
}

/**
 * A spawner that hands each start to `script`, which decides how that process behaves (it may end
 * it at once, hold it open, or refuse to start it by returning `'refuse'`).
 */
export function fakeSpawner(
  script: (started: Started, all: readonly Started[]) => unknown,
  kill: KillBehavior = 'dies',
): FakeSpawner {
  const started: Started[] = [];
  const layer = Layer.succeed(ProcessSpawner, {
    spawn: (command, args, env) =>
      Effect.suspend(() => {
        const entry: Started = {command, args, env, process: fakeProcess(kill)};
        started.push(entry);
        if (invokeCallback(() => script(entry, started)) === 'refuse')
          return Effect.fail(new SpawnFailed({command, message: 'refused'}));
        return Effect.succeed(entry.process.process);
      }),
  });
  return {layer, started};
}
