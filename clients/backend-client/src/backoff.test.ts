import {describe, expect, it} from 'bun:test';
import {BackoffSchedule, DEFAULT_RECONNECT_DELAYS_MS} from './backoff.js';

describe('BackoffSchedule', () => {
  it('yields the schedule in order, then exhausts', () => {
    const backoff = new BackoffSchedule([10, 20, 30]);
    expect(backoff.next()).toBe(10);
    expect(backoff.next()).toBe(20);
    expect(backoff.exhausted).toBe(false);
    expect(backoff.next()).toBe(30);
    expect(backoff.exhausted).toBe(true);
    expect(backoff.next()).toBeUndefined();
  });

  it('resets to the start so a fresh outage backs off from the beginning', () => {
    const backoff = new BackoffSchedule([10, 20]);
    backoff.next();
    backoff.next();
    expect(backoff.next()).toBeUndefined();
    backoff.reset();
    expect(backoff.exhausted).toBe(false);
    expect(backoff.next()).toBe(10);
  });

  it('treats an empty schedule as exhausted from the start', () => {
    const backoff = new BackoffSchedule([]);
    expect(backoff.exhausted).toBe(true);
    expect(backoff.next()).toBeUndefined();
  });

  it('defaults to the shared reconnect schedule', () => {
    expect(new BackoffSchedule().next()).toBe(DEFAULT_RECONNECT_DELAYS_MS[0]);
  });
});
