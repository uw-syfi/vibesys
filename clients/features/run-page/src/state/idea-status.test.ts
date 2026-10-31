/** An idea's status from typed facts: closed, never a raw identifier, never read from prose. */
import {describe, expect, test} from 'bun:test';
import {FAILURE_REASONS} from '@vibesys/run-fixtures';
import type {Candidate, CandidateMeasurement} from '@vibesys/run-state/candidates';
import fc from 'fast-check';
import type {Change} from './headline.js';
import {IDEA_STATUSES, ideaStatus, STATUS_WORDS} from './idea-status.js';
import {idea} from './timeline-fixtures.js';

const changes: fc.Arbitrary<Change | null> = fc.constantFrom('faster', 'slower', 'same', null);

function failed(reason: string | null, measurement: CandidateMeasurement | null = null): Candidate {
  return {...idea('f', 'failed', 0, 60), reason, measurement};
}

describe('the status of an idea', () => {
  test('is one of the closed set for any outcome, reason and change', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('running', 'passed', 'failed'),
        fc.oneof(fc.constantFrom(...FAILURE_REASONS), fc.string(), fc.constant(null)),
        changes,
        (outcome, reason, change) => {
          const found = ideaStatus({...idea('i', outcome, 0, 60), reason}, change);
          expect(IDEA_STATUSES).toContain(found.status);
          expect(found.status === 'running').toBe(outcome === 'running');
        },
      ),
    );
  });

  test('names a cause that is words, not an identifier, and not the status again', () => {
    for (const reason of FAILURE_REASONS) {
      const found = ideaStatus(failed(reason), null);
      if (found.cause === null) continue;
      expect(found.cause).not.toMatch(/[_-]/);
      expect(found.cause.toLowerCase()).not.toBe(STATUS_WORDS[found.status].toLowerCase());
    }
  });

  test('says no cause for a reason that is no identifier: prose is never shown as a cause', () => {
    fc.assert(
      fc.property(
        fc.string().filter(text => !/^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$/.test(text)),
        text => {
          expect(ideaStatus(failed(text), null).cause).toBeNull();
        },
      ),
    );
  });

  test('reads the cause of the reasons the screen words by hand', () => {
    expect(ideaStatus(failed('turned_down'), null)).toEqual({
      status: 'dropped',
      cause: 'Turned down',
    });
    expect(ideaStatus(failed('implementation-failed'), null)).toEqual({
      status: 'failed',
      cause: 'Not implemented',
    });
    expect(ideaStatus(failed('review-rejected'), null)).toEqual({
      status: 'failed',
      cause: 'Review rejected',
    });
    expect(ideaStatus(failed('not_measured'), null).status).toBe('not-measured');
  });

  test('a passed idea is faster, no gain by what it measured, or not measured', () => {
    const passed = idea('p', 'passed', 0, 60);
    expect(ideaStatus(passed, 'faster').status).toBe('faster');
    expect(ideaStatus(passed, 'same').status).toBe('no-gain');
    expect(ideaStatus(passed, 'slower').status).toBe('no-gain');
    expect(ideaStatus(passed, null).status).toBe('not-measured');
  });
});
