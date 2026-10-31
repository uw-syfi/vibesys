import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {EMPTY_TRACE_LOG, traceStep} from './live.js';
import {heroView} from './headline.js';
import {objectiveCharts} from './objective-chart.js';
import {createRunReader} from './run-model.js';

/**
 * The Run screen's words over generated multi runs, read from their span events: every idea starts
 * from the input or an earlier idea (its `base_round`), the run has exactly one best (the idea the
 * adoption span names, adopted once the run ended) and the chart's last new best is that idea; and
 * nothing the screen words says accepted, kept, rejected or turned down.
 */

const ROOT = 'flow:search/#1';
const REASONS = ['turned_down', 'not_measured', 'no_design', 'not_opened'] as const;
const OLD_WORDS = /accepted|kept|rejected|turned down/i;
const INPUT = 100;

interface Round {
  readonly n: number;
  readonly passed: boolean;
  readonly reason: string | null;
  /** The round whose idea it starts from; null: the input. */
  readonly base: number | null;
  /** What its trusted gate measured; null when no gate measured it. */
  readonly value: number | null;
}

function workstream(id: string, n: number, label: NonNullable<Span['label']>): Span {
  return {
    id,
    parent: ROOT,
    type: 'flow',
    name: 'workstream',
    status: 'done',
    started_at: n * 100,
    ended_at: n * 100 + 50,
    label,
  };
}

function roundsOf(random: Seeded): Round[] {
  const count = 1 + random.int(10);
  const rounds: Round[] = [];
  for (let n = 1; n <= count; n += 1) {
    const passed = random.int(3) > 0;
    const measured = random.int(5) > 0;
    const earlier = rounds.filter(round => round.passed);
    const base = earlier.length === 0 || random.int(3) === 0 ? null : random.pick(earlier).n;
    // distinct values, so "the best" is never a tie
    const value = measured ? INPUT / 2 + n * 7 + random.int(7) : null;
    rounds.push({n, passed, reason: passed ? null : random.pick(REASONS), base, value});
  }
  return rounds;
}

/** What the search names best: the best measured passed idea that beats the input, else the input. */
function leader(rounds: readonly Round[]): Round | null {
  const beating = rounds.filter(r => r.passed && r.value !== null && r.value > INPUT);
  return beating.reduce<Round | null>(
    (best, r) => (best === null || (r.value ?? 0) > (best.value ?? 0) ? r : best),
    null,
  );
}

function gateOf(round: Round, value: number): Span {
  return {
    id: `op:r${round.n}/gate`,
    parent: `flow:r${round.n}/#1`,
    type: 'operation',
    name: 'measure',
    status: 'done',
    started_at: round.n * 100 + 10,
    ended_at: round.n * 100 + 20,
    label: {
      kind: 'gate',
      title: 'benchmark',
      verdict: value > INPUT ? 'passed' : 'slower',
      attrs_json: JSON.stringify({
        stage: 'benchmark',
        metric_value: value,
        vs_baseline: value / INPUT,
      }),
    },
  };
}

function spansOf(rounds: readonly Round[], adopted: boolean): Span[] {
  const best = leader(rounds);
  const spans: Span[] = [
    {id: ROOT, parent: null, type: 'flow', name: 'search', status: 'running', started_at: 0},
    {
      id: 'op:adopt',
      parent: ROOT,
      type: 'operation',
      name: 'adopt',
      status: adopted ? 'done' : 'running',
      started_at: 1,
      label: {
        kind: 'adoption',
        title: 'adopt',
        ...(adopted ? {verdict: 'adopted'} : {}),
        attrs_json: JSON.stringify({candidate: best === null ? null : `c${best.n}`}),
      },
    },
  ];
  for (const round of rounds) {
    const id = `flow:r${round.n}/#1`;
    spans.push(
      workstream(id, round.n, {
        kind: 'workstream',
        title: `idea ${round.n}`,
        verdict: round.passed ? 'passed' : 'failed',
        ...(round.reason === null ? {} : {reason: round.reason}),
        links: round.base === null ? [] : [{rel: 'builds_on', to: `flow:r${round.base}/#1`}],
        attrs_json: JSON.stringify({
          hypothesis_id: `h${round.n}`,
          round: round.n,
          base_round: round.base,
          idea: `idea ${round.n}`,
          revision: round.passed ? `c${round.n}` : null,
        }),
      }),
    );
    if (round.value !== null) spans.push(gateOf(round, round.value));
  }
  return spans;
}

function modelOf(spans: readonly Span[]) {
  const events = spans.map(
    (each, index) =>
      ({
        protocol_version: 1,
        run_id: 'run',
        sequence: index + 1,
        timestamp: '2026-10-16T00:00:00Z',
        type: 'span_started',
        data: {kind: 'span_started', span: each},
      }) as RunEvent,
  );
  return createRunReader().read(traceStep(EMPTY_TRACE_LOG, {kind: 'events', events}), null);
}

function isMark(badge: {readonly label: string}): boolean {
  return badge.label === 'best' || badge.label === 'adopted';
}

/** Red when failed; grey when passed without a new best (slower than the input never is one). */
function expectedTone(round: Round, onLine: boolean): 'accent' | 'muted' | 'err' {
  if (!round.passed) return 'err';
  return onLine && (round.value ?? 0) > INPUT ? 'accent' : 'muted';
}

const EVERYTHING = {...IDEAS_FILTER_DEFAULT, failed: true};

describe('the Run screen words over generated runs', () => {
  test('every idea starts from the input or an earlier idea, and says best when that idea led', () => {
    for (const seed of SEEDS) {
      const rounds = roundsOf(new Seeded(seed));
      const {rows} = ideasView(modelOf(spansOf(rounds, false)), EVERYTHING);
      for (const row of rows) {
        const round = rounds.find(each => row.title === `idea ${each.n}`) as Round;
        if (round.base === null) {
          expect(row.trail).toEqual({kind: 'input', label: 'input', parent: null});
          continue;
        }
        const leading = rounds
          .filter(each => each.n < round.n && each.passed && each.value !== null)
          .reduce<Round | null>(
            (top, each) => (top === null || (each.value ?? 0) > (top.value ?? 0) ? each : top),
            null,
          );
        const parent = `flow:r${round.base}/#1`;
        expect(row.trail).toEqual(
          leading?.n === round.base
            ? {kind: 'best', label: 'best', parent}
            : {kind: 'idea', label: 'a passed idea', parent},
        );
        expect(round.base).toBeLessThan(round.n);
      }
    }
  });

  test('exactly one idea is the best, the one the adoption span names, and adopted once over', () => {
    for (const seed of SEEDS) {
      const rounds = roundsOf(new Seeded(seed));
      const best = leader(rounds);
      for (const adopted of [false, true]) {
        const model = modelOf(spansOf(rounds, adopted));
        const marked = ideasView(model, EVERYTHING).rows.filter(row => row.badges.some(isMark));
        const word = adopted ? 'adopted' : 'best';
        if (best === null) {
          expect(marked).toEqual([]);
          continue;
        }
        expect(
          marked.map(row => [row.title, row.badges.filter(isMark).map(badge => badge.label)]),
        ).toEqual([[`idea ${best.n}`, [word]]]);
        const chart = stripChartView(model, 0, null);
        const onLine = chart?.points.filter(point => point.tone === 'accent') ?? [];
        expect(onLine.at(-1)?.id).toBe(`flow:r${best.n}/#1`);
      }
    }
  });

  test('a passed idea that does not beat the best is grey on the chart, a failed one red', () => {
    for (const seed of SEEDS) {
      const rounds = roundsOf(new Seeded(seed));
      const chart = stripChartView(modelOf(spansOf(rounds, false)), 0, null);
      for (const point of chart?.points ?? []) {
        const round = rounds.find(each => `flow:r${each.n}/#1` === point.id) as Round;
        expect(point.tone).toBe(expectedTone(round, point.tone === 'accent'));
      }
    }
  });

  test('no word on the idea rows, the hero or the chart says accepted, kept, rejected or turned down', () => {
    for (const seed of SEEDS) {
      const rounds = roundsOf(new Seeded(seed));
      for (const adopted of [false, true]) {
        const model = modelOf(spansOf(rounds, adopted));
        const view = ideasView(model, EVERYTHING);
        const chart = stripChartView(model, 0, null);
        const words = [
          JSON.stringify(view.rows),
          JSON.stringify(heroView(model)),
          JSON.stringify(chart?.points.map(point => point.label) ?? []),
          ...Object.keys(view.counts),
        ].join(' ');
        expect(words).not.toMatch(OLD_WORDS);
        for (const row of view.rows) expect(row.state.label).toMatch(/^(Passed|Failed|Running)/);
      }
    }
  });
});
