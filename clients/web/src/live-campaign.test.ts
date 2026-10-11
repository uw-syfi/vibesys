import {expect, test} from 'bun:test';
import {connectionFreshnessAt} from './live-campaign.js';

test('no frame received yet reports stale, since nothing proves the stream is live', () => {
  expect(connectionFreshnessAt(1_000, null, 500)).toBe('stale');
});

test('a frame within the threshold reports fresh', () => {
  expect(connectionFreshnessAt(1_000, 600, 500)).toBe('fresh');
});

test('a frame exactly at the threshold still reports fresh', () => {
  expect(connectionFreshnessAt(1_000, 500, 500)).toBe('fresh');
});

test('a frame older than the threshold reports stale', () => {
  expect(connectionFreshnessAt(1_000, 100, 500)).toBe('stale');
});

test('uses the default threshold when none is given', () => {
  expect(connectionFreshnessAt(10_000, 9_000)).toBe('fresh');
  expect(connectionFreshnessAt(100_000, 9_000)).toBe('stale');
});
