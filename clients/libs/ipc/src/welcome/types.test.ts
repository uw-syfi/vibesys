import {describe, expect, test} from 'bun:test';
import {attempt, settled, UNEXPECTED_FAILURE, type WelcomeResult} from './types.js';

describe('settled', () => {
  test('a reply passes through unchanged', async () => {
    const reply: WelcomeResult<number> = {ok: false, error: 'no', authNeeded: true};
    expect(await settled(Promise.resolve(reply))).toBe(reply);
    expect(await settled(Promise.resolve({ok: true, value: 3}))).toEqual({ok: true, value: 3});
  });

  test('a rejected call becomes a failed result naming why', async () => {
    for (const reason of [new Error('main process exited'), 'main process exited']) {
      expect(await settled(Promise.reject(reason))).toEqual({
        ok: false,
        error: 'main process exited',
        authNeeded: false,
      });
    }
  });
});

describe('attempt', () => {
  test('an answer of the main process passes through, a reported failure with its own words', async () => {
    const reported: WelcomeResult<number> = {ok: false, error: 'host-a refused', authNeeded: true};
    expect(await attempt(() => Promise.resolve(reported))).toBe(reported);
    expect(await attempt(() => Promise.resolve({ok: true, value: 3}))).toEqual({
      ok: true,
      value: 3,
    });
  });

  test('a call that throws or rejects is an unexpected failure that carries none of the raw error', async () => {
    const raw = 'bridge.harnesses is not a function';
    const throwing = (): Promise<WelcomeResult<number>> => {
      throw new TypeError(raw);
    };
    for (const call of [
      throwing,
      () => Promise.reject(new Error(raw)),
      () => Promise.reject(raw),
    ]) {
      const result = await attempt(call);
      expect(result).toEqual({
        ok: false,
        error: UNEXPECTED_FAILURE,
        authNeeded: false,
        unexpected: true,
      });
      expect(JSON.stringify(result)).not.toContain(raw);
    }
  });
});
