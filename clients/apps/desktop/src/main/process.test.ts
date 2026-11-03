import type {ChildProcess} from 'node:child_process';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as TestClock from 'effect/testing/TestClock';
import {fakeProcess, fakeSpawner} from '../testing/main/fake-process.js';
import {fromChild, retire, spawnScoped} from './process.js';

/** The Node child lifecycle boundary, without a real process or signal. */
class FakeNodeChild extends EventEmitter {
  readonly stdin = new PassThrough();
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  readonly signals: NodeJS.Signals[] = [];
  readonly pid: number | undefined;
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;

  constructor(pid: number | undefined) {
    super();
    this.pid = pid;
  }

  /** Node's error-only answer when the operating system refuses a signal. */
  kill(signal: NodeJS.Signals): boolean {
    this.signals.push(signal);
    this.emit('error', new Error(`could not send ${signal}`));
    return false;
  }

  exit(code: number | null): void {
    this.exitCode = code;
    this.stdout.end();
    this.stderr.end();
    this.emit('exit', code, null);
    this.emit('close', code, null);
  }

  asChildProcess(): ChildProcess {
    return this as unknown as ChildProcess;
  }
}

describe('retiring a process', () => {
  it.effect('confirms a process that dies on TERM without waiting out the grace', () =>
    Effect.gen(function* () {
      const fake = fakeProcess('dies');
      expect(yield* retire(fake.process)).toBe('confirmed');
      expect(fake.signals).toEqual(['SIGTERM']);
    }),
  );

  it.effect('sends KILL one grace after TERM to a process that ignores it', () =>
    Effect.gen(function* () {
      const fake = fakeProcess('ignores');
      const retiring = yield* Effect.forkChild(retire(fake.process));
      yield* Effect.yieldNow;
      expect(fake.signals).toEqual(['SIGTERM']);
      yield* TestClock.adjust('1 second');
      expect(fake.signals).toEqual(['SIGTERM', 'SIGKILL']);
      fake.end(null);
      expect(yield* Fiber.join(retiring)).toBe('confirmed');
    }),
  );

  it.effect('reports a process that never dies as unconfirmed after two graces', () =>
    Effect.gen(function* () {
      const fake = fakeProcess('ignores');
      const retiring = yield* Effect.forkChild(retire(fake.process));
      yield* Effect.yieldNow;
      yield* TestClock.adjust('2 seconds');
      expect(yield* Fiber.join(retiring)).toBe('unconfirmed');
    }),
  );

  it.effect('confirms a dead process whose output a descendant still holds, one grace later', () =>
    Effect.gen(function* () {
      const fake = fakeProcess('dies-holding-pipes');
      fake.stdout.resume();
      fake.stderr.resume();
      const retiring = yield* Effect.forkChild(retire(fake.process));
      yield* Effect.yieldNow;
      yield* TestClock.adjust('1 second');
      expect(yield* Fiber.join(retiring)).toBe('confirmed');
      fake.releasePipes();
    }),
  );

  it.effect('ends a scoped process when the scope closes, and only then', () =>
    Effect.gen(function* () {
      const spawner = fakeSpawner(() => {});
      yield* Effect.scoped(
        Effect.gen(function* () {
          yield* spawnScoped('ssh', ['-V']);
          expect(spawner.started[0]?.process.signals).toEqual([]);
        }),
      ).pipe(Effect.provide(spawner.layer));
      expect(spawner.started).toHaveLength(1);
      expect(spawner.started[0]?.process.signals).toEqual(['SIGTERM']);
    }),
  );
});

describe('a Node child as a process', () => {
  it.effect('has exited, with no future event, when the child already has', () =>
    Effect.gen(function* () {
      const exited = new FakeNodeChild(42);
      exited.exitCode = 17;
      expect(yield* fromChild(exited.asChildProcess()).exit).toBe(17);
      const signalled = new FakeNodeChild(43);
      signalled.signalCode = 'SIGTERM';
      expect(yield* fromChild(signalled.asChildProcess()).exit).toBeNull();
    }),
  );

  it.effect('ends at once when the child could not be spawned', () =>
    Effect.gen(function* () {
      const child = new FakeNodeChild(undefined);
      const process = fromChild(child.asChildProcess());
      child.emit('error', new Error('could not spawn'));
      expect(yield* process.exit).toBeNull();
    }),
  );

  it.effect('does not take a refused signal for an exit, and reports the exit that follows', () =>
    Effect.gen(function* () {
      const child = new FakeNodeChild(41);
      const process = fromChild(child.asChildProcess());
      const retiring = yield* Effect.forkChild(retire(process));
      yield* Effect.yieldNow;
      yield* TestClock.adjust('2 seconds');
      expect(yield* Fiber.join(retiring)).toBe('unconfirmed');
      expect(child.signals).toEqual(['SIGTERM', 'SIGKILL']);
      child.exit(null);
      expect(yield* process.exit).toBeNull();
    }),
  );

  it.effect('is terminated when the child exited but has not closed its pipes', () =>
    Effect.gen(function* () {
      const child = new FakeNodeChild(50);
      const process = fromChild(child.asChildProcess());
      child.emit('exit', 0, null);
      yield* process.terminated;
    }),
  );
});
