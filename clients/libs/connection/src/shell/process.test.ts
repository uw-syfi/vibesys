import {describe, expect, test} from 'bun:test';
import type {ChildProcess} from 'node:child_process';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {FakeClock} from '@vibesys/transport/testing';
import {fakeProcess, pipeHeldProcess} from '../testing/fake-process.js';
import {fromChild, retireProcess} from './process.js';

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

  /** Model Node's error-only answer when the operating system refuses a signal. */
  kill(signal: NodeJS.Signals): boolean {
    this.signals.push(signal);
    this.emit('error', new Error(`could not send ${signal}`));
    return false;
  }

  failToSpawn(): void {
    this.emit('error', new Error('could not spawn'));
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

describe('Node child process lifecycle evidence', () => {
  test('a pre-spawn error settles because no child was created', async () => {
    const child = new FakeNodeChild(undefined);
    const process = fromChild(child.asChildProcess());

    child.failToSpawn();

    expect(await process.exit).toBeNull();
    expect(process.running).toBeFalse();
  });

  test('kill errors cannot confirm retirement of a started child', async () => {
    const clock = new FakeClock();
    const child = new FakeNodeChild(41);
    const process = fromChild(child.asChildProcess());
    const retirement = retireProcess(process, clock.scheduleTimeout, 100);

    expect(process.running).toBeTrue();
    expect(child.signals).toEqual(['SIGTERM']);
    clock.advanceBy(200);

    expect(await retirement.outcome).toEqual({kind: 'unconfirmed'});
    expect(process.running).toBeTrue();
    expect(child.signals).toEqual(['SIGTERM', 'SIGKILL']);

    child.exit(null);
    expect(await retirement.exit).toBeNull();
    expect(process.running).toBeFalse();
  });

  test('already-exited child state is authoritative without a future event', async () => {
    const normallyExitedChild = new FakeNodeChild(42);
    normallyExitedChild.exitCode = 17;
    const normallyExitedProcess = fromChild(normallyExitedChild.asChildProcess());

    expect(normallyExitedProcess.running).toBeFalse();
    expect(await normallyExitedProcess.exit).toBe(17);

    const signaledChild = new FakeNodeChild(43);
    signaledChild.signalCode = 'SIGTERM';
    const signaledProcess = fromChild(signaledChild.asChildProcess());

    expect(signaledProcess.running).toBeFalse();
    expect(await signaledProcess.exit).toBeNull();
  });
});

describe('retirement of a process whose descendant holds its pipes', () => {
  test('a dead process is confirmed though its output has not ended', async () => {
    const clock = new FakeClock();
    const held = pipeHeldProcess();
    held.stdout.resume();
    held.stderr.resume();
    const retirement = retireProcess(held.process, clock.scheduleTimeout, 100);

    await held.process.terminated;
    clock.advanceBy(100);

    expect(await retirement.outcome).toEqual({kind: 'confirmed', code: null, pipesHeld: true});
    held.releasePipes();
    expect(await retirement.exit).toBeNull();
  });

  test('a process that never dies is still unconfirmed', async () => {
    const clock = new FakeClock();
    const stuck = fakeProcess();
    const retirement = retireProcess(
      {...stuck.process, kill: () => {}, terminated: new Promise<void>(() => {})},
      clock.scheduleTimeout,
      100,
    );

    clock.advanceBy(200);

    expect(await retirement.outcome).toEqual({kind: 'unconfirmed'});
  });

  test('a node child that exited but has not closed is terminated', async () => {
    const child = new FakeNodeChild(50);
    const process = fromChild(child.asChildProcess());

    child.emit('exit', 0, null);

    await process.terminated;
    expect(process.running).toBeFalse();
  });
});
