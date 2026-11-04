/**
 * Starting and ending processes: the `ProcessSpawner` role, its Node implementation over
 * `child_process`, and bounded retirement. Every process the main process starts (an ssh control
 * command, a channel to a gateway, the local gateway's starter) goes through this role, so the
 * code above it runs against in-memory processes.
 */
import {type ChildProcess, spawn} from 'node:child_process';
import {homedir} from 'node:os';
import type {Readable, Writable} from 'node:stream';
import * as Context from 'effect/Context';
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Layer from 'effect/Layer';
import * as Option from 'effect/Option';
import type * as Scope from 'effect/Scope';
import {SpawnFailed} from './errors.js';

export type KillSignal = 'SIGTERM' | 'SIGKILL';

/** A started process as the main process observes it. */
export interface SpawnedProcess {
  readonly stdin: Writable;
  readonly stdout: Readable;
  readonly stderr: Readable;
  /**
   * The exit code once the process has ended and its output has been read; null when it ended by
   * signal or never started.
   */
  readonly exit: Effect.Effect<number | null>;
  /**
   * Completes once the process itself is dead, without waiting for its output to end. `exit` can
   * stay pending after that: a descendant the process started (ssh's ProxyCommand) may keep an
   * inherited pipe open. Retirement confirms on this, because it asks whether the process is gone,
   * not whether every pipe is.
   */
  readonly terminated: Effect.Effect<void>;
  readonly kill: (signal: KillSignal) => Effect.Effect<void>;
}

/** Runs a program with arguments and extra environment variables. */
export class ProcessSpawner extends Context.Service<
  ProcessSpawner,
  {
    readonly spawn: (
      command: string,
      args: readonly string[],
      env: Readonly<Record<string, string>>,
    ) => Effect.Effect<SpawnedProcess, SpawnFailed>;
  }
>()('app/ProcessSpawner') {}

/** What bounded retirement proved about the exact process. */
type Retirement = 'confirmed' | 'unconfirmed';

/** How long retirement waits after each signal. */
const TERMINATION_GRACE = '1 second';

/**
 * End `process`: TERM now, KILL after one grace, and `unconfirmed` after a second. A process that
 * `terminated` but whose output stays open (a descendant holds the pipes) is confirmed one grace
 * after it died.
 */
export const retire = Effect.fn('Process.retire')(function* (process: SpawnedProcess) {
  const gone = Effect.raceFirst(
    process.exit,
    Effect.andThen(process.terminated, Effect.sleep(TERMINATION_GRACE)),
  );
  yield* process.kill('SIGTERM');
  if (Option.isSome(yield* Effect.timeoutOption(gone, TERMINATION_GRACE)))
    return 'confirmed' as Retirement;
  yield* process.kill('SIGKILL');
  const confirmed = yield* Effect.timeoutOption(gone, TERMINATION_GRACE);
  return (Option.isSome(confirmed) ? 'confirmed' : 'unconfirmed') as Retirement;
});

/** Start a process that the enclosing scope retires when it closes, however it closes. */
export const spawnScoped = (
  command: string,
  args: readonly string[],
  env: Readonly<Record<string, string>> = {},
): Effect.Effect<SpawnedProcess, SpawnFailed, ProcessSpawner | Scope.Scope> =>
  Effect.gen(function* () {
    const spawner = yield* ProcessSpawner;
    return yield* Effect.acquireRelease(spawner.spawn(command, args, env), process =>
      Effect.asVoid(retire(process)),
    );
  });

/** A real child process spawned with three pipes. */
export function fromChild(child: ChildProcess): SpawnedProcess {
  const alreadyExited = child.exitCode !== null || child.signalCode !== null;
  const terminated = Deferred.makeUnsafe<void>();
  const exited = Deferred.makeUnsafe<number | null>();
  if (alreadyExited) {
    Deferred.doneUnsafe(terminated, Exit.void);
    Deferred.doneUnsafe(exited, Exit.succeed(child.exitCode));
  } else {
    // `error` is not an exit event: Node also emits it when `kill()` cannot signal a process that is
    // still live. Only the pre-spawn form proves there is no child whose exit remains to be
    // observed. `pid` covers a successful spawn whose `spawn` event preceded adaptation.
    let spawned = child.pid !== undefined;
    child.once('spawn', () => {
      spawned = true;
    });
    // Keep observing after a post-spawn error: retirement can try TERM and then KILL, and each
    // failed signal may emit its own error.
    child.on('error', () => {
      if (spawned || child.pid !== undefined) return;
      Deferred.doneUnsafe(terminated, Exit.void);
      Deferred.doneUnsafe(exited, Exit.succeed(null));
    });
    child.once('exit', () => Deferred.doneUnsafe(terminated, Exit.void));
    child.once('close', code => {
      Deferred.doneUnsafe(terminated, Exit.void);
      Deferred.doneUnsafe(exited, Exit.succeed(code));
    });
  }
  // A write to a closed pipe must not become an uncaught error; the owner of a channel adds its own
  // listener and reports the fault.
  child.stdin?.on('error', () => {});
  return {
    // `stdio: 'pipe'` guarantees all three streams.
    stdin: child.stdin as Writable,
    stdout: child.stdout as Readable,
    stderr: child.stderr as Readable,
    exit: Deferred.await(exited),
    terminated: Deferred.await(terminated),
    kill: signal =>
      Effect.sync(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill(signal);
      }),
  };
}

/** The real operating system. */
export const NodeProcessSpawner: Layer.Layer<ProcessSpawner> = Layer.succeed(ProcessSpawner, {
  spawn: (command, args, env) =>
    Effect.try({
      try: () =>
        fromChild(
          spawn(command, args, {
            stdio: 'pipe',
            env: {...process.env, HOME: process.env['HOME'] ?? homedir(), ...env},
          }),
        ),
      catch: cause =>
        new SpawnFailed({
          command,
          message: cause instanceof Error ? cause.message : String(cause),
        }),
    }),
});
