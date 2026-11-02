import {describe, expect, test} from 'bun:test';
import type {WatchRunsResponse} from '@vibesys/orchestrator-proto';
import {portsOf} from './ports.js';
import type {CancelSignal, OrchestratorSource} from './source.js';

const unused = (): never => {
  throw new Error('this test does not call it');
};

/** A source whose run stream yields `count` frames, then waits until its signal aborts. */
function waitingSource(count: number, seen: {aborted: number}): OrchestratorSource {
  return {
    async *watchRuns(
      _after: bigint,
      signal: CancelSignal,
    ): AsyncIterable<WatchRunsResponse['body']> {
      for (let sent = 0; sent < count; sent++) {
        yield {case: undefined} as WatchRunsResponse['body'];
      }
      // A signal that is already aborted never fires again, so a source checks before it listens.
      if (!signal.aborted)
        await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve()));
      seen.aborted += 1;
    },
    watchAgent: unused,
    listItems: unused,
    listPlans: unused,
    dispatch: unused,
  };
}

describe('the ports of a source', () => {
  test('closing a watch ends its frames quietly, once, however often it is closed', async () => {
    for (const count of [0, 1, 5]) {
      const seen = {aborted: 0};
      const watch = portsOf(waitingSource(count, seen)).openRuns(0n);
      const frames: unknown[] = [];
      const reading = (async () => {
        for await (const frame of watch.frames) {
          frames.push(frame);
          if (frames.length === count) watch.close();
        }
      })();
      if (count === 0) watch.close();
      await reading;
      watch.close();
      expect(frames.length).toBe(count);
      expect(seen.aborted).toBe(1);
    }
  });

  test('closing the ports releases what carries the source', () => {
    let released = 0;
    const ports = portsOf(waitingSource(0, {aborted: 0}), () => {
      released += 1;
    });
    ports.close();
    expect(released).toBe(1);
  });
});
