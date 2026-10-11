import {describe, expect, test} from 'bun:test';
import type {ScheduleTimeout} from '@vibesys/backend-client';
import {bootstrapGateway, type Fetch} from './backend.js';

const CAPABILITY = 'http://127.0.0.1:8765/?token=secret';

class ManualScheduler {
  readonly #pending: Array<{callback: () => void; cancelled: boolean; delayMs: number}> = [];

  readonly scheduleTimeout: ScheduleTimeout = (callback, delayMs) => {
    const pending = {callback, cancelled: false, delayMs};
    this.#pending.push(pending);
    return () => {
      pending.cancelled = true;
    };
  };

  runNext(): void {
    const pending = this.#pending.shift();
    if (pending === undefined) throw new Error('No deadline is scheduled');
    if (!pending.cancelled) pending.callback();
  }

  activeDelays(): number[] {
    return this.#pending.filter(pending => !pending.cancelled).map(pending => pending.delayMs);
  }
}

describe('bootstrapGateway', () => {
  test('uses a credentialed no-store request with a bounded lifetime', async () => {
    const scheduler = new ManualScheduler();
    const calls: Array<{url: string; init: RequestInit | undefined}> = [];
    const successful: Fetch = async (url, init) => {
      calls.push({url, init});
      return new Response('', {
        status: 200,
        headers: {'X-VibeSys-Browser-Session': 'browser-session'},
      });
    };

    expect(
      await bootstrapGateway(CAPABILITY, {
        fetcher: successful,
        scheduleTimeout: scheduler.scheduleTimeout,
        timeoutMs: 25,
      }),
    ).toBe('browser-session');
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(CAPABILITY);
    expect(calls[0]?.init).toMatchObject({
      cache: 'no-store',
      credentials: 'include',
      mode: 'cors',
    });
    expect(calls[0]?.init?.signal).toBeInstanceOf(AbortSignal);
    expect(scheduler.activeDelays()).toEqual([]);

    const rejected: Fetch = async () => new Response('', {status: 403});
    await expect(
      bootstrapGateway(CAPABILITY, {
        fetcher: rejected,
        scheduleTimeout: scheduler.scheduleTimeout,
      }),
    ).rejects.toThrow('HTTP 403');
  });

  test('aborts a stalled adapter at the injected deadline', async () => {
    const scheduler = new ManualScheduler();
    const requestSignals: AbortSignal[] = [];
    const stalled: Fetch = (_url, init) => {
      if (init?.signal !== null && init?.signal !== undefined) requestSignals.push(init.signal);
      return new Promise<Response>(() => undefined);
    };
    const exchange = bootstrapGateway(CAPABILITY, {
      fetcher: stalled,
      scheduleTimeout: scheduler.scheduleTimeout,
      timeoutMs: 25,
    });

    expect(scheduler.activeDelays()).toEqual([25]);
    scheduler.runNext();

    await expect(exchange).rejects.toThrow('Gateway connection timed out after 25 ms');
    expect(requestSignals[0]?.aborted).toBe(true);
    expect(scheduler.activeDelays()).toEqual([]);
  });

  test('settles on caller cancellation even when the fetch adapter ignores its signal', async () => {
    const scheduler = new ManualScheduler();
    const owner = new AbortController();
    const requestSignals: AbortSignal[] = [];
    const stalled: Fetch = (_url, init) => {
      if (init?.signal !== null && init?.signal !== undefined) requestSignals.push(init.signal);
      return new Promise<Response>(() => undefined);
    };
    const exchange = bootstrapGateway(CAPABILITY, {
      fetcher: stalled,
      scheduleTimeout: scheduler.scheduleTimeout,
      signal: owner.signal,
    });

    owner.abort(new Error('attempt superseded'));

    await expect(exchange).rejects.toThrow('attempt superseded');
    expect(requestSignals[0]?.aborted).toBe(true);
    expect(scheduler.activeDelays()).toEqual([]);
  });
});
