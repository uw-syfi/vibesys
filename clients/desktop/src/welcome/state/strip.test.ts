import {describe, expect, test} from 'bun:test';
import {RUN_ID_UNAVAILABLE, resumeDecision} from '../../resume-run.js';
import {randomChrome} from '../../testing/fake-chrome.js';
import {Seeded} from '../../testing/seeded.js';
import type {ChromeState} from '../../welcome-protocol.js';
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
  if (controls.stop) expect(attached?.instanceId).not.toBeNull();
}

describe('stripControls', () => {
  test('regression: an ended run offers Resume only for its exact id, and never Retry', () => {
    const random = new Seeded(3);
    for (let round = 0; round < 500; round += 1) checkStrip(randomChrome(random));
  });

  test('an ended run with no id says why Resume is unavailable', () => {
    const state: ChromeState = {
      mode: 'welcome',
      attached: {
        host: 'local',
        hostLabel: 'This Mac',
        instanceId: '0123456789ab',
        runId: null,
        project: '/p',
        connection: {kind: 'run-ended'},
      },
      stops: {},
    };
    expect(stripControls(state).resume).toEqual({kind: 'unavailable', reason: RUN_ID_UNAVAILABLE});
  });
});
