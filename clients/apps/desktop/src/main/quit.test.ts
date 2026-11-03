import {expect, it} from 'vitest';
import {quit} from './quit.js';

it('exits on cleanup or the independent deadline', async () => {
  for (const winner of ['release', 'deadline'] as const) {
    let release = () => {};
    let deadline = () => {};
    let exited = 0;
    let timedOut = 0;
    let cancelled = 0;
    const done = quit({
      release: () =>
        new Promise<void>(resolve => {
          release = resolve;
        }),
      deadline: () => ({
        elapsed: new Promise<void>(resolve => {
          deadline = resolve;
        }),
        cancel: () => {
          cancelled += 1;
        },
      }),
      exit: () => {
        exited += 1;
      },
      timedOut: () => {
        timedOut += 1;
      },
    });
    if (winner === 'release') release();
    else deadline();
    await done;
    expect(exited).toBe(1);
    expect(cancelled).toBe(1);
    expect(timedOut).toBe(winner === 'deadline' ? 1 : 0);
  }
});
