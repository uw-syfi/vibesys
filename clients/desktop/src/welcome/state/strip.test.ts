import {describe, expect, test} from 'bun:test';
import {randomChrome} from '../../testing/fake-chrome.js';
import {Seeded} from '../../testing/seeded.js';
import type {ChromeState} from '../../welcome-protocol.js';
import {runLink, stripControls} from './strip.js';

/** The strip's controls agree with the window state they come from. */
function checkStrip(state: ChromeState): void {
  const controls = stripControls(state);
  if (controls.resume) {
    expect([controls.retry, controls.stop, controls.force]).toEqual([false, false, false]);
  }
  if (state.attached !== null && runLink(state.attached.connection) === 'ended')
    expect(controls.resume).toBe(true);
  expect(controls.retry).toBe(
    state.attached !== null && runLink(state.attached.connection) === 'stuck' && !controls.resume,
  );
  if (state.attached === null) expect(Object.values(controls).some(Boolean)).toBe(false);
  expect(controls.back).toBe(state.attached !== null && state.mode === 'welcome');
  if (controls.stop) expect(state.attached?.instanceId).not.toBeNull();
}

describe('stripControls', () => {
  test('regression: an ended run offers Resume and never Retry', () => {
    const random = new Seeded(3);
    for (let round = 0; round < 500; round += 1) checkStrip(randomChrome(random));
  });
});
