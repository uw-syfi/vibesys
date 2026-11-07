/**
 * A report that answered someone's Catch me up is for that viewer's window: it must never become the
 * run's summary, however new it is, and the lead's own reports still are.
 */
import {describe, expect, it} from '@effect/vitest';
import {CheckInState} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {checkInOf, reportMessageOf} from '../../../testing/orchestrator/check-ins.js';
import {emptyState, type OrchestratorState} from '../state.js';
import {latestReport} from './summary.js';

const RUN = 'run-1';
const AT = Date.parse('2026-11-01T18:00:00Z');

interface Posted {
  /** Its number: the host numbers the lead's reports in the order it records them. */
  readonly sequence: number;
  /** Whether it answered a viewer's catch-up. */
  readonly answer: boolean;
  readonly viewer: 'v1' | 'v2';
}

const posted = fc.uniqueArray(
  fc.record({
    sequence: fc.integer({min: 1, max: 40}),
    answer: fc.boolean(),
    viewer: fc.constantFrom('v1' as const, 'v2' as const),
  }),
  {selector: report => report.sequence, maxLength: 12},
);

/** The state the run stream folds: every report as a message, each answer linked from a check-in. */
function stateOf(reports: readonly Posted[]): OrchestratorState {
  const messages = reports.map(report =>
    reportMessageOf(
      `report-${report.sequence}`,
      RUN,
      AT + report.sequence * 1000,
      `Report ${report.sequence}`,
      '',
      report.sequence,
    ),
  );
  const checkIns = reports
    .filter(report => report.answer)
    .map(report =>
      checkInOf({
        id: `ci-${report.sequence}`,
        runId: RUN,
        viewer: report.viewer,
        sinceMs: AT,
        atMs: AT + report.sequence * 1000,
        state: CheckInState.READY,
        ask: `ask-${report.sequence}`,
        report: `report-${report.sequence}`,
      }),
    );
  return {
    ...emptyState,
    messages: Object.fromEntries(messages.map(message => [message.id, message])),
    checkIns: Object.fromEntries(checkIns.map(checkIn => [checkIn.id, checkIn])),
  };
}

describe('the summary of a run', () => {
  it('is the newest report the lead posted on its own, never a catch-up answer, however new', () => {
    fc.assert(
      fc.property(posted, reports => {
        const own = reports.filter(report => !report.answer).map(report => report.sequence);
        const summary = latestReport(stateOf(reports), RUN);
        expect(summary?.id).toBe(own.length === 0 ? undefined : `report-${Math.max(...own)}`);
      }),
      {numRuns: 200},
    );
  });

  it('keeps the lead summary when a catch-up answer is newer than it', () => {
    const state = stateOf([
      {sequence: 3, answer: false, viewer: 'v1'},
      {sequence: 4, answer: true, viewer: 'v2'},
    ]);
    expect(latestReport(state, RUN)?.headline).toBe('Report 3');
  });

  it('leaves out the answers to every viewer, not only the one looking', () => {
    const state = stateOf([
      {sequence: 1, answer: false, viewer: 'v1'},
      {sequence: 2, answer: true, viewer: 'v1'},
      {sequence: 3, answer: true, viewer: 'v2'},
    ]);
    expect(latestReport(state, RUN)?.id).toBe('report-1');
  });

  it('is a report no check-in links, as it is for the moment before the link arrives', () => {
    // The report is committed before the check-in that links it, so for that moment it is the newest.
    const state = stateOf([{sequence: 5, answer: false, viewer: 'v1'}]);
    expect(latestReport(state, RUN)?.id).toBe('report-5');
  });

  it('is not hidden by a check-in of another run', () => {
    const own = stateOf([{sequence: 5, answer: false, viewer: 'v1'}]);
    const elsewhere = checkInOf({
      id: 'ci-x',
      runId: 'run-2',
      viewer: 'v1',
      sinceMs: AT,
      atMs: AT,
      state: CheckInState.READY,
      ask: 'ask-x',
      report: 'report-5',
    });
    const state = {...own, checkIns: {[elsewhere.id]: elsewhere}};
    expect(latestReport(state, RUN)?.id).toBe('report-5');
  });
});
