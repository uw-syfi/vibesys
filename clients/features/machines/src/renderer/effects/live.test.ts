import {expect, test} from 'bun:test';
import {FakeSubscriptionPort} from '@vibesys/subscription/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {createLiveStats} from './live.js';

function rig() {
  const clock = new FakeClock();
  const live = createLiveStats(new FakeSubscriptionPort(), clock.schedule, () => clock.now());
  return {clock, live};
}

test('nothing is scheduled until a screen watches the clock, and nothing after the last leaves', () => {
  const {clock, live} = rig();
  expect(clock.pending).toBe(0);
  expect(live.clock.getState()).toBeNull();
  clock.advanceBy(60_000);
  const stop = live.clock.subscribe(() => {});
  expect(clock.pending).toBe(1);
  stop();
  expect(clock.pending).toBe(0);
});

test('a watched clock reads the viewer’s time every second, for every watcher', () => {
  const {clock, live} = rig();
  const heard: (number | null)[] = [];
  const first = live.clock.subscribe(() => heard.push(live.clock.getState()));
  const second = live.clock.subscribe(() => {});
  clock.advanceBy(3_000);
  expect(live.clock.getState()).toBe(3_000);
  // The reading at the start, then each second's, reached the listener in order.
  expect(heard).toEqual([0, 1_000, 2_000, 3_000]);
  first();
  // One watcher is enough to keep it going, and the last one's leaving stops it.
  clock.advanceBy(2_000);
  expect(live.clock.getState()).toBe(5_000);
  second();
  expect(clock.pending).toBe(0);
  clock.advanceBy(10_000);
  // At rest it holds no reading: an old one would be shown as an age that is not the age.
  expect(live.clock.getState()).toBeNull();
});

test('watching again after a rest reads the time at once, not the last one it held', () => {
  const {clock, live} = rig();
  live.clock.subscribe(() => {})();
  expect(live.clock.getState()).toBeNull();
  clock.advanceBy(60_000);
  const stop = live.clock.subscribe(() => {});
  expect(live.clock.getState()).toBe(60_000);
  stop();
});
