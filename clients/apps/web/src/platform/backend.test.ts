import {describe, expect, test} from 'bun:test';
import type {ScheduleTimeout} from '@vibesys/transport';
import {
  bootstrapGateway,
  EventSourceCampaignStream,
  type Fetch,
  fetchCampaignFixture,
} from './backend.js';

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

describe('campaign browser boundary', () => {
  test('validates fixture data and forwards owner cancellation', async () => {
    const controller = new AbortController();
    const calls: RequestInit[] = [];
    const fetcher: Fetch = async (_url, init) => {
      if (init !== undefined) calls.push(init);
      return Response.json({schemaVersion: 99});
    };
    await expect(fetchCampaignFixture(controller.signal, fetcher)).rejects.toThrow();
    expect(calls[0]?.signal).toBe(controller.signal);
    expect(calls[0]?.cache).toBe('no-store');
    await expect(
      fetchCampaignFixture(controller.signal, async () => new Response('', {status: 503})),
    ).rejects.toThrow('503');
  });

  test('fails closed for malformed JSON and strictly invalid campaign frames', () => {
    const cases = [
      {label: 'malformed JSON', data: '{bad json'},
      {
        label: 'invalid campaign frame',
        data: JSON.stringify({kind: 'status', seq: 1, status: 'unknown'}),
      },
    ] as const;

    for (const {label, data} of cases) {
      const source = new ManualCampaignSource();
      const frames: unknown[] = [];
      const errors: string[] = [];
      const stream = new EventSourceCampaignStream('/campaign', () => source);
      const close = stream.subscribe(
        frame => frames.push(frame),
        error => errors.push(error.message),
      );
      const staleMessage = source.onmessage;
      const staleError = source.onerror;

      staleMessage?.({data});

      expect({label, closeCalls: source.closeCalls, errors: errors.length}).toEqual({
        label,
        closeCalls: 1,
        errors: 1,
      });
      expect(source.onmessage).toBeNull();
      expect(source.onerror).toBeNull();

      staleMessage?.({
        data: JSON.stringify({kind: 'status', seq: 1, status: 'completed'}),
      });
      source.readyState = 2;
      staleError?.({});
      close();
      close();

      expect({label, frames, errors: errors.length, closeCalls: source.closeCalls}).toEqual({
        label,
        frames: [],
        errors: 1,
        closeCalls: 1,
      });
    }
  });

  test('keeps native reconnects live and releases a terminal source once', () => {
    const source = new ManualCampaignSource();
    const frames: unknown[] = [];
    const errors: string[] = [];
    const stream = new EventSourceCampaignStream('/campaign', () => source);
    const close = stream.subscribe(
      frame => frames.push(frame),
      error => errors.push(error.message),
    );
    const staleMessage = source.onmessage;
    const staleError = source.onerror;

    source.readyState = 0;
    staleError?.({});
    staleMessage?.({
      data: JSON.stringify({kind: 'status', seq: 1, status: 'completed'}),
    });
    expect(frames).toEqual([{kind: 'status', seq: 1, status: 'completed'}]);
    expect(errors).toEqual([]);
    expect(source.closeCalls).toBe(0);

    source.readyState = 2;
    staleError?.({});
    staleError?.({});
    staleMessage?.({
      data: JSON.stringify({kind: 'status', seq: 2, status: 'completed'}),
    });
    close();
    close();

    expect(frames).toEqual([{kind: 'status', seq: 1, status: 'completed'}]);
    expect(errors).toEqual(['Campaign stream connection error']);
    expect(source.closeCalls).toBe(1);
    expect(source.onmessage).toBeNull();
    expect(source.onerror).toBeNull();
  });

  test('owner disposal is idempotent and fences queued source callbacks', () => {
    const source = new ManualCampaignSource();
    const frames: unknown[] = [];
    const errors: string[] = [];
    const stream = new EventSourceCampaignStream('/campaign', () => source);
    const close = stream.subscribe(
      frame => frames.push(frame),
      error => errors.push(error.message),
    );
    const staleMessage = source.onmessage;
    const staleError = source.onerror;

    close();
    close();
    staleMessage?.({
      data: JSON.stringify({kind: 'status', seq: 1, status: 'completed'}),
    });
    source.readyState = 2;
    staleError?.({});

    expect(frames).toEqual([]);
    expect(errors).toEqual([]);
    expect(source.closeCalls).toBe(1);
    expect(source.onmessage).toBeNull();
    expect(source.onerror).toBeNull();
  });
});

class ManualCampaignSource {
  onmessage: ((event: {readonly data: string}) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  readyState = 1;
  closeCalls = 0;
  close(): void {
    this.closeCalls += 1;
  }
}
