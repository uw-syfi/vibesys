/** Canonical scenario declarations, projected into native goldens by Python. */
import {committedFixtures} from './catalog.js';

export const FAILURE_REASONS: readonly string[] = [
  'turned_down',
  'not_measured',
  'no_design',
  'not_opened',
  'stopped',
  'parent-unverified',
  'refused',
  'ended',
  'out-of-budget',
  'turn-failed',
  'invalid-reply',
  'render-failed',
  'wait-refused',
  'checkpoint-declined',
  'no-changed-candidate',
  'implementation-failed',
  'review-rejected',
  'no-trusted-evidence',
  'evidence-refused',
  'evidence-undecodable',
  'profile-capture-unavailable',
  'turn-limit',
  'repeated-turn',
  'merge-refused',
  'merge-unsound',
  'merge-regressed',
];

/** Every shared scenario, validated from its canonical declaration. */
export function scenarios() {
  return committedFixtures().filter(fixture => fixture.name !== 'queue-rs-live');
}
