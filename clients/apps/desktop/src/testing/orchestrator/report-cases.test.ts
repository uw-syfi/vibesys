/** The three report cases, folded as the app folds them: what each says is what the fixture page shows. */
import {describe, expect, it} from '@effect/vitest';
import {RunStatus} from '@vibesys/orchestrator-proto';
import type {CostView} from '../../state/index.js';
import {
  agentCostRows,
  applyChange,
  applyCost,
  applySearch,
  emptyState,
  runReport,
} from '../../state/index.js';
import {ENDED_RUN, NO_PRICES, PRICED_AND_UNPRICED} from './cost-views.js';
import {REPORT_CASES, type ReportCase, reportCaseWorld, SERIES_LIMIT} from './index.js';
import {FAILED_COST, STARTED_COST, WEEK_COST} from './report-cases.js';
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

  it('counts the agents one way on the whole screen: the facts line, the table and the host’s summary agree', () => {
    for (const name of REPORT_CASES) {
      const {cost} = reportCaseWorld(name);
      const {agents, facts} = reportOf(name);
      const rows = agentCostRows(agents, cost);
      const sum = (pick: (row: (typeof rows)[number]) => number): number =>
        rows.reduce((all, row) => all + pick(row), 0);
      expect(
        sum(row => row.total),
        name,
      ).toBe(facts.agents.total);
      expect(
        sum(row => row.running),
        name,
      ).toBe(facts.agents.running);
      // Every type the cost view lists is a type the host's summary counts, with the same agents.
      for (const role of cost.roles) {
        expect(
          agents.types.find(type => type.role === role.role)?.total,
          `${name} ${role.role}`,
        ).toBe(role.agents);
      }
    }
  });

  it('gives the three cases a lead report of three lengths: none, short with an empty field, very long', () => {
    expect(reportOf('started').summary).toBeUndefined();
    const short = reportOf('failed').summary;
    expect(short?.headline.length).toBeLessThan(80);
    expect(short?.fields.map(field => field.field)).toEqual(['inFlight']);
    const long = reportOf('week').summary;
    expect(long?.headline.length).toBeGreaterThan(300);
    expect(long?.fields).toHaveLength(3);
    expect(long?.fields.some(field => /\S{100,}/.test(field.text))).toBe(true);
  });

  it('measures the week like a real search: most points well under the best, across the whole run', () => {
    const [throughput] = reportOf('week').objectives;
    if (throughput === undefined) throw new Error('the case has its objective');
    const best = throughput.bestValue ?? 0;
    const others = throughput.points.filter(point => !point.bestSoFar);
    const below = others.filter(point => point.value < 0.95 * best);
    expect(below.length).toBeGreaterThan(0.6 * others.length);
    // The points are in every sixth of the run, not in one blob.
    const [first, last] = [throughput.points[0]?.atMs ?? 0, throughput.points.at(-1)?.atMs ?? 0];
    const sixths = new Set(
      throughput.points.map(point => Math.floor(((point.atMs - first) / (last - first + 1)) * 6)),
    );
    expect(sixths.size).toBe(6);
  });

  it('has the week’s ideas made by the agents the run has, and some of them very long', () => {
    const report = reportOf('week');
    expect(new Set(report.ideas?.ideas.map(idea => idea.agent)).size).toBeGreaterThan(1);
    expect(report.ideas?.ideas.some(idea => idea.title.length > 120)).toBe(true);
  });
});

/** What a cost view says of a type must agree with itself, or a screen cannot say it plainly. */
function expectConsistentTypes(view: CostView): void {
  for (const role of view.roles) {
    expect(role.unpricedTurns).toBeLessThanOrEqual(role.turns);
    // A type has no cost only when none of its turns was priced.
    if (role.usage.costUsd === null) expect(role.unpricedTurns).toBe(role.turns);
  }
}

describe('the costs the cases and the named views serve', () => {
  it('give every type a cost that agrees with its turns: no cost means no priced turn', () => {
    for (const view of [
      STARTED_COST,
      FAILED_COST,
      WEEK_COST,
      PRICED_AND_UNPRICED,
      ENDED_RUN,
      NO_PRICES,
    ]) {
      expectConsistentTypes(view);
    }
  });

  it('add up: the types hold the run’s turns, unpriced turns and cost', () => {
    for (const view of [STARTED_COST, FAILED_COST, WEEK_COST]) {
      const sum = (pick: (role: CostView['roles'][number]) => number): number =>
        view.roles.reduce((total, role) => total + pick(role), 0);
      expect(sum(role => role.turns)).toBe(view.turns);
      expect(sum(role => role.unpricedTurns)).toBe(view.unpricedTurns);
      expect(sum(role => role.usage.costUsd ?? 0)).toBeCloseTo(view.total.costUsd ?? 0, 6);
    }
  });
});

describe('the week’s ideas', () => {
  it('differ from one another: no title twice, workstreams of different sizes and different best gains', () => {
    const report = reportOf('week');
    const titles = report.ideas?.ideas.map(idea => idea.title) ?? [];
    // The two long ones the fixture repeats on purpose are the only repeats.
    expect(titles.length - new Set(titles).size).toBeLessThanOrEqual(1);
    const sizes = new Set(
      (report.ideas?.workstreams ?? []).map(w => w.kept + w.rejected + w.open + w.parked),
    );
    expect(sizes.size).toBeGreaterThan(2);
    const gains = new Set((report.ideas?.workstreams ?? []).map(w => w.gains[0]?.ratio.toFixed(2)));
    expect(gains.size).toBeGreaterThan(2);
  });
});
