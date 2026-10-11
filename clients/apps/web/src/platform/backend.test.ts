import {describe, expect, test} from 'bun:test';
import type {ScheduleTimeout} from '@vibesys/transport';
import {bootstrapGateway, type Fetch, fetchReplayFixture} from './backend.js';

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

/** Load a replay fixture over `fetcher`: its events, or the message it failed with. */
async function load(fetcher: () => Promise<Response>) {
  try {
    return {events: await fetchReplayFixture('replay.jsonl', fetcher), failure: null};
  } catch (error) {
    return {events: [], failure: error instanceof Error ? error.message : String(error)};
  }
}

describe('fetchReplayFixture', () => {
  test('loads JSONL events', async () => {
    const {events, failure} = await load(
      async () =>
        new Response('{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n'),
    );
    expect(failure).toBeNull();

    expect(events).toHaveLength(1);
  });

  test('reports the HTTP status', async () => {
    const {events, failure} = await load(async () => new Response('unavailable', {status: 503}));
    expect(failure).toBe('Replay fixture request failed with 503');
    expect(events).toHaveLength(0);
  });

  test('reports the line containing malformed replay JSON', async () => {
    const {events, failure} = await load(
      async () =>
        new Response(
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n{\n',
        ),
    );
    expect(failure).toBe('Replay fixture contains invalid JSON on line 2');
    expect(events).toHaveLength(0);
  });

  test('rejects one malformed event atomically and names its line and field', async () => {
    const {events, failure} = await load(
      async () =>
        new Response(
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n' +
            '{"type":"server_ready","sequence":2}\n',
        ),
    );
    expect(failure).toBe(
      'Replay fixture contains invalid event on line 2: Invalid server run event: timestamp must be a string',
    );
    expect(events).toHaveLength(0);
  });

  test('rejects malformed fields inside known event data before folding any line', async () => {
    const {events, failure} = await load(
      async () =>
        new Response(
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n' +
            '{"type":"agent_execution_started","sequence":2,"timestamp":"2026-09-27T00:00:01Z","data":{"kind":"agent_execution_started","stage":"implement"}}\n',
        ),
    );
    expect(failure).toBe(
      'Replay fixture contains invalid event on line 2: Invalid server run event.data: activity must be present',
    );
    expect(events).toHaveLength(0);
  });

  test('accepts unknown closed-set members with valid wire kinds', async () => {
    const {events, failure} = await load(
      async () =>
        new Response(
          JSON.stringify({
            protocol_version: 1,
            sequence: 1,
            timestamp: '2026-09-27T00:00:00Z',
            type: 'future-event-type',
            status: 'future-status',
            data: {kind: 'future-data-kind', future_field: true},
          }),
        ),
    );
    expect(failure).toBeNull();

    expect(events).toHaveLength(1);
  });

  test('rejects an incompatible event protocol version', async () => {
    const {events, failure} = await load(
      async () =>
        new Response(
          '{"protocol_version":2,"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n',
        ),
    );
    expect(failure).toBe(
      'Replay fixture contains invalid event on line 1: Invalid server run event: protocol_version must be 1',
    );
    expect(events).toHaveLength(0);
  });
});
