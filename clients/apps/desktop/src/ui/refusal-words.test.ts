import {describe, expect, it} from '@effect/vitest';
import {isRetryable, Refused} from '../core/errors.js';
import {faultOf} from '../core/fault.js';
import {REFUSAL_CAUSES} from '../state/index.js';
import {HOST_STATUS_LABELS} from './host-status-words.js';
import {causeHint, causeWord} from './refusal-words.js';

/** Words a status chip already uses for a link or a feed, which no refusal may be taken for. */
const LINK_WORDS = [
  ...HOST_STATUS_LABELS.filter(label => label !== 'Refused'),
  'Not read yet',
  'No runs',
  'Reconnecting',
  'Connecting',
];

describe('every cause a host or the app refuses for', () => {
  it('has its own short label, never blank and never a word of a link that is only slow', () => {
    const labels = REFUSAL_CAUSES.map(causeWord);
    expect(new Set(labels).size).toBe(REFUSAL_CAUSES.length);
    for (const label of labels) {
      expect(label.trim()).not.toBe('');
      expect(label.split(' ').length).toBeLessThanOrEqual(4);
      expect(LINK_WORDS).not.toContain(label);
      // A label is a few words, not a sentence, and names no command-line flag.
      expect(label).not.toMatch(/[.:]|--/);
    }
  });

  it('has a hover that names no flag, and is a fault no retry tries again', () => {
    for (const cause of REFUSAL_CAUSES) {
      expect(causeHint(cause).trim(), cause).not.toBe('');
      expect(causeHint(cause), cause).not.toMatch(/--/);
      const error = new Refused({cause, detail: 'in the refusing side\u2019s words'});
      expect(isRetryable(error), cause).toBe(false);
      expect(faultOf(error), cause).toMatchObject({kind: 'Refused', cause, retryable: false});
    }
  });
});
