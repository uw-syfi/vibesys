import {describe, expect, test} from 'bun:test';
import {Seeded} from '@vibesys/core-state/testing';
import {resumeDecision} from '../../../common/resume-run.js';
import type {ChromeState} from '../../../common/welcome-protocol.js';
import {randomChrome} from '../../../testing/fake-chrome.js';
import {runLink, stripControls} from './strip.js';

/** The strip's controls agree with the window state they come from. */
function checkStrip(state: ChromeState): void {
  const controls = stripControls(state);
  const ended = controls.resume !== null;
  if (ended) expect([controls.retry, controls.stop, controls.force]).toEqual([false, false, false]);
  const attached = state.attached;
  if (attached !== null && runLink(attached.connection) === 'ended') {
    expect(controls.resume).toEqual(resumeDecision(attached.runId));
  }
  expect(controls.retry).toBe(
    attached !== null && runLink(attached.connection) === 'stuck' && !ended,
  );
  if (attached === null) expect(Object.values(controls).some(Boolean)).toBe(false);
  expect(controls.back).toBe(attached !== null && state.mode === 'welcome');
}

describe('stripControls', () => {
  test('regression: an ended run offers Resume only for its exact id, and never Retry', () => {
    const random = new Seeded(3);
    for (let round = 0; round < 500; round += 1) checkStrip(randomChrome(random));
  });
});
