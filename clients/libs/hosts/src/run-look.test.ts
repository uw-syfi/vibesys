import {describe, expect, test} from 'bun:test';
import type {HostRun, RunEnd} from './index.js';
import {endedLook} from './run-look.js';

type Outcome = NonNullable<RunEnd>['outcome'];

/** Every outcome an end record can carry: a new one fails to compile here until it is listed. */
const OUTCOMES: Readonly<Record<Outcome, true>> = {
  adopted: true,
  'kept-input': true,
  'no-winner': true,
  deadline: true,
  'finish-refused': true,
  'adoption-failed': true,
  failed: true,
  stopped: true,
  force_stopped: true,
};

const ended = (outcome: Outcome): Pick<HostRun, 'phase' | 'end'> => ({
  phase: 'completed',
  end: {outcome, reason: 'why', resumable: false},
});

describe('endedLook', () => {
  test('only an outcome that adopted or kept a result is a success', () => {
    const ok = Object.keys(OUTCOMES).filter(
      outcome => endedLook(ended(outcome as Outcome))?.tone === 'ok',
    );
    expect(ok.sort()).toEqual(['adopted', 'kept-input']);
  });

  test('an ending that is not a success says its outcome, in a tone that is not ok', () => {
    expect(endedLook(ended('no-winner'))).toEqual({
      kind: 'finished',
      label: 'No winner',
      tone: 'neutral',
    });
    expect(endedLook(ended('finish-refused'))?.tone).toBe('warn');
    expect(endedLook(ended('deadline'))?.tone).toBe('warn');
    expect(endedLook(ended('failed'))?.tone).toBe('err');
    expect(endedLook(ended('adoption-failed'))?.tone).toBe('err');
    expect(endedLook(ended('stopped'))?.tone).toBe('neutral');
    expect(endedLook(ended('force_stopped'))?.tone).toBe('neutral');
  });

  test('an ending that adopted an uncompared candidate says so and is never a plain success', () => {
    const uncompared = (outcome: Outcome): Pick<HostRun, 'phase' | 'end'> => ({
      phase: 'completed',
      end: {outcome, reason: 'why', resumable: false, uncompared: true},
    });
    expect(endedLook(uncompared('adopted'))).toEqual({
      kind: 'finished',
      label: 'Finished · adopted without an input comparison',
      tone: 'warn',
    });
    expect(endedLook(uncompared('deadline'))).toEqual({
      kind: 'finished',
      label: 'Time limit reached · adopted without an input comparison',
      tone: 'warn',
    });
    expect(endedLook(uncompared('stopped'))).toEqual({
      kind: 'stopped',
      label: 'Stopped · adopted without an input comparison',
      tone: 'warn',
    });
  });

  test('every outcome has its own label', () => {
    const labels = Object.keys(OUTCOMES).map(
      outcome => endedLook(ended(outcome as Outcome))?.label,
    );
    expect(new Set(labels).size).toBe(labels.length);
  });

  test('a tombstone with no end record reads from its last phase; other phases say nothing', () => {
    expect(endedLook({phase: 'completed', end: null})?.tone).toBe('ok');
    expect(endedLook({phase: 'failed', end: null})?.tone).toBe('err');
    expect(endedLook({phase: 'stopped', end: null})?.tone).toBe('neutral');
    expect(endedLook({phase: 'running', end: null})).toBeNull();
  });
});
