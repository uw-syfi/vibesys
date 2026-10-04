import {describe, it} from 'node:test';
import {expect} from '../test-support/expect.js';
import {FakeClock} from './fake-clock.test-helper.js';

describe('FakeClock', () => {
  it('does not fire a timer cancelled while an awaiting runner observes it', async () => {
    const clock = new FakeClock();
    let calls = 0;
    const cancel = clock.schedule(() => {
      calls += 1;
    }, 5);

    const running = clock.runNext(5);
    cancel();
    await running;

    expect(calls).toBe(0);
    expect(clock.pendingDelays()).toEqual([]);
  });
});
