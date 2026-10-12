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

describe('AppLaunches', () => {
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
});
