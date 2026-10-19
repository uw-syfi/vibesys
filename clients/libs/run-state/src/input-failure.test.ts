import {describe, expect, it} from 'bun:test';
import type {Diagnostic} from '@vibesys/contracts';
import {event as fixtureEvent} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEvent} from './index.js';

/** The once-per-run event a failed input measurement emits (S173), with the server's diagnostic. */
function inputFailed(sequence: number, compared: boolean, diagnostic: Diagnostic | null) {
  return fixtureEvent(sequence, 'input_measurement_failed', {
    status: 'failed',
    diagnostic,
    data: {
      kind: 'input_measurement_failed',
      failure: 'accuracy-failed',
      stage: 'correctness',
      summary: 'accuracy stage exceeded its 1200 s limit',
      attempts: 2,
      compared,
    },
  });
}

const DIAGNOSTIC: Diagnostic = {
  code: 'input_measurement_failed',
  summary: "The input's trusted measurement failed (accuracy-failed, stage correctness)",
  detail: 'accuracy stage exceeded its 1200 s limit',
  scope: 'run',
  severity: 'error',
  source: 'loop',
};

describe('a failed input measurement', () => {
  it('starts with none', () => {
    expect(initialCoreState().inputFailure).toBeNull();
  });

  for (const compared of [true, false]) {
    it(`is kept as the run's input failure (compared: ${compared})`, () => {
      const state = reduceEvent(initialCoreState(), inputFailed(1, compared, DIAGNOSTIC));
      expect(state.inputFailure).toEqual({
        failure: 'accuracy-failed',
        stage: 'correctness',
        summary: 'accuracy stage exceeded its 1200 s limit',
        attempts: 2,
        compared,
      });
    });
  }

  it('is shown through its diagnostic, not as transcript prose', () => {
    const state = reduceEvent(initialCoreState(), inputFailed(1, false, DIAGNOSTIC));
    expect(state.diagnostics.map(item => item.code)).toContain('input_measurement_failed');
    expect(state.transcript).toEqual([]);
  });
});
