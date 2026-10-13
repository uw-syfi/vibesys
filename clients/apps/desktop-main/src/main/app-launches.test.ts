import {describe, expect, test} from 'bun:test';
import {AppLaunches, type LaunchTarget} from './app-launches.js';

class FakeTarget implements LaunchTarget<string> {
  readonly events: string[] = [];
  readonly failures = new Map<string, Error>();

  focus(): void {
    this.events.push('focus');
  }

  async open(plan: string): Promise<void> {
    this.events.push(`open:${plan}`);
    const failure = this.failures.get(plan);
    if (failure !== undefined) throw failure;
  }
}

class Deferred {
  readonly promise: Promise<void>;
  #resolve: () => void = () => {};
  #reject: (error: Error) => void = () => {};

  constructor() {
    this.promise = new Promise((resolve, reject) => {
      this.#resolve = resolve;
      this.#reject = reject;
    });
  }

  resolve(): void {
    this.#resolve();
  }

  reject(error: Error): void {
    this.#reject(error);
  }
}

class ControlledTarget implements LaunchTarget<string> {
  readonly events: string[] = [];
  readonly opens = new Map<string, Deferred>();

  focus(): void {
    this.events.push('focus');
  }

  open(plan: string): Promise<void> {
    this.events.push(`open:${plan}`);
    const opened = new Deferred();
    this.opens.set(plan, opened);
    return opened.promise;
  }
}

async function settleImmediateLaunches(count: number): Promise<void> {
  for (let settled = 0; settled < count; settled += 1) await Promise.resolve();
}

describe('AppLaunches', () => {
  test('regression: asynchronous launches complete one at a time in FIFO order', async () => {
    const failures: Error[] = [];
    const launches = new AppLaunches<string>(error => failures.push(error));
    const target = new ControlledTarget();

    launches.second('second-a');
    launches.second('second-b');
    const ready = launches.ready(target, 'initial');
    expect(target.events).toEqual(['open:initial']);

    target.opens.get('initial')?.resolve();
    await ready;
    expect(target.events).toEqual(['open:initial', 'focus', 'open:second-a']);

    launches.second('second-c');
    expect(target.events).toEqual(['open:initial', 'focus', 'open:second-a']);

    const failure = new Error('second-a failed');
    target.opens.get('second-a')?.reject(failure);
    await Promise.resolve();
    expect(failures).toEqual([failure]);
    expect(target.events).toEqual([
      'open:initial',
      'focus',
      'open:second-a',
      'focus',
      'open:second-b',
    ]);

    target.opens.get('second-b')?.resolve();
    await Promise.resolve();
    expect(target.events).toEqual([
      'open:initial',
      'focus',
      'open:second-a',
      'focus',
      'open:second-b',
      'focus',
      'open:second-c',
    ]);
    target.opens.get('second-c')?.resolve();
    await Promise.resolve();
    expect(failures).toEqual([failure]);
  });

  test('a failed initial launch never starts retained second launches', async () => {
    const launches = new AppLaunches<string>(() => {});
    const target = new ControlledTarget();
    const failure = new Error('initial failed');

    launches.second('second');
    const ready = launches.ready(target, 'initial');
    target.opens.get('initial')?.reject(failure);

    await expect(ready).rejects.toBe(failure);
    expect(target.events).toEqual(['open:initial']);
  });

  test('regression: second launches received before readiness are opened once in order', async () => {
    const failures: Error[] = [];
    const launches = new AppLaunches<string>(error => failures.push(error));
    const target = new FakeTarget();

    launches.second('second-a');
    launches.second('second-b');
    expect(target.events).toEqual([]);
    await launches.ready(target, 'initial');

    expect(target.events).toEqual([
      'open:initial',
      'focus',
      'open:second-a',
      'focus',
      'open:second-b',
    ]);
    expect(failures).toEqual([]);
  });

  test('every second launch is focused and opened once across the readiness boundary', async () => {
    for (let readyAfter = 0; readyAfter <= 8; readyAfter += 1) {
      const launches = new AppLaunches<string>(() => {});
      const target = new FakeTarget();
      const plans = Array.from({length: 8}, (_, index) => `second-${index}`);
      for (const plan of plans.slice(0, readyAfter)) launches.second(plan);
      const initial = launches.ready(target, 'initial');
      for (const plan of plans.slice(readyAfter)) launches.second(plan);
      await initial;
      await settleImmediateLaunches(plans.length);

      expect(target.events.filter(event => event.startsWith('open:'))).toEqual([
        'open:initial',
        ...plans.map(plan => `open:${plan}`),
      ]);
      expect(target.events.filter(event => event === 'focus')).toHaveLength(plans.length);
    }
  });

  test('one second launch failure is reported without dropping later launches', async () => {
    const failures: Error[] = [];
    const launches = new AppLaunches<string>(error => failures.push(error));
    const target = new FakeTarget();
    const failure = new Error('could not attach');
    target.failures.set('broken', failure);

    await launches.ready(target, 'initial');
    launches.second('broken');
    launches.second('later');
    await Promise.resolve();

    expect(target.events).toEqual(['open:initial', 'focus', 'open:broken', 'focus', 'open:later']);
    expect(failures).toEqual([failure]);
  });

  test('a throwing failure reporter cannot reject the detached drain or strand later launches', async () => {
    const reports: Error[] = [];
    const launches = new AppLaunches<string>(error => {
      reports.push(error);
      throw new Error('reporter failed');
    });
    const target = new FakeTarget();
    const failure = new Error('could not attach');
    target.failures.set('broken', failure);

    await launches.ready(target, 'initial');
    launches.second('broken');
    launches.second('later');
    await settleImmediateLaunches(2);

    expect(target.events).toEqual(['open:initial', 'focus', 'open:broken', 'focus', 'open:later']);
    expect(reports).toEqual([failure]);
  });
});
