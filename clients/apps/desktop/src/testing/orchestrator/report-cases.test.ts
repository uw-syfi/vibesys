/** The three report cases, folded as the app folds them: what each says is what the fixture page shows. */
import {describe, expect, it} from '@effect/vitest';
import {RunStatus} from '@vibesys/orchestrator-proto';
import {applyChange, applyCost, applySearch, emptyState, runReport} from '../../state/index.js';
import {REPORT_CASES, type ReportCase, reportCaseWorld, SERIES_LIMIT} from './index.js';
import {serveSummaries} from './served-summary.js';

function reportOf(name: ReportCase) {
  const {scenario, search, cost} = reportCaseWorld(name);
  const state = serveSummaries(
    applyCost(applySearch(scenario.changes.reduce(applyChange, emptyState), search), {
      ...cost,
      runId: scenario.runId,
    }),
  );
  const report = runReport(state, scenario.runId, scenario.endMs + 60_000);
  if (report === undefined) throw new Error('the case has its run');
  return report;
}

describe('the report cases', () => {
  it('names three cases, each a run the host serves', () => {
    expect(REPORT_CASES).toEqual(['started', 'failed', 'week']);
    for (const name of REPORT_CASES) expect(reportOf(name).runId).not.toBe('');
  });

  it('has a run that has just started with nothing measured, no idea and one agent', () => {
    const report = reportOf('started');
    expect(report.header.status).toBe(RunStatus.RUNNING);
    expect(report.facts.measured.count).toBe(0);
    expect(report.facts.ideas).toEqual({count: 0, atLeast: false});
    expect(report.facts.agents.total).toBe(1);
    expect(report.objectives).toEqual([]);
  });

  it('has a run that failed within minutes, with three measurements and an unpriced turn', () => {
    const report = reportOf('failed');
    expect(report.header.status).toBe(RunStatus.FAILED);
    expect(report.header.elapsedMs).toBeLessThan(5 * 60_000);
    expect(report.facts.measured.count).toBe(3);
    expect(report.facts.spend?.floor).toBe(true);
    expect(report.objectives).toHaveLength(1);
  });

  it('has a run of six days with two objectives, the host’s cap on what it serves, and dozens of ideas', () => {
    const report = reportOf('week');
    expect(report.header.elapsedMs).toBeGreaterThan(6 * 24 * 3_600_000);
    expect(report.objectives).toHaveLength(2);
    for (const objective of report.objectives) {
      expect(objective.points.length).toBeLessThanOrEqual(SERIES_LIMIT);
      // The newest best is kept whatever is cut.
      expect(objective.bestPath.length).toBeGreaterThan(0);
    }
    expect(report.truncated).toBe(true);
    expect(report.facts.measured.atLeast).toBe(true);
    expect(report.ideas?.ideas.length).toBe(64);
    expect(report.latest).toHaveLength(8);
  });
});
