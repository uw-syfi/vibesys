import {describe, expect, test} from 'bun:test';
import {bytesWords, historyLoadingView} from './history-words.js';

describe('the loading screen words', () => {
  test('say "n of N events" with a share once the total is known', () => {
    expect(historyLoadingView({events: 1200, bytes: 3_400_000, total: 2436})).toEqual({
      title: 'Reading the run’s history…',
      detail: '1,200 of 2,436 events · 3.4 MB read',
      share: 1200 / 2436,
    });
  });

  test('never claim more than the total, and never go backwards as events arrive', () => {
    let last = -1;
    for (let events = 0; events <= 3000; events += 7) {
      const share = historyLoadingView({events, bytes: 0, total: 2436}).share ?? -1;
      expect(share).toBeGreaterThanOrEqual(last);
      expect(share).toBeLessThanOrEqual(1);
      last = share;
    }
  });

  test('without a total give events and bytes so far and no bar', () => {
    expect(historyLoadingView({events: 500, bytes: 640_000, total: null})).toEqual({
      title: 'Reading the run’s history…',
      detail: '500 events · 640 KB read so far',
      share: null,
    });
    expect(historyLoadingView({events: 0, bytes: 0, total: null}).detail).toBe(
      'Waiting for the first events',
    );
  });

  test('size words carry a unit at every magnitude', () => {
    expect(bytesWords(12)).toBe('12 B');
    expect(bytesWords(2048)).toBe('2 KB');
    expect(bytesWords(8_015_340)).toBe('8.0 MB');
  });
});
