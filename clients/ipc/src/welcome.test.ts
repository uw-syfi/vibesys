import {describe, expect, test} from 'bun:test';
import {settled, type WelcomeResult} from './welcome.js';

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
