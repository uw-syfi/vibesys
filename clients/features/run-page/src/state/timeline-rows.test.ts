import {describe, expect, test} from 'bun:test';
import type {
  Candidate,
  CandidateAttempt,
  CandidateOutcome,
  CandidateStep,
} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {roleSeries} from './role-series.js';
import {attempt, idea, modelOf, step} from './timeline-fixtures.js';
import {
  attemptLines,
  attemptStrip,
  FAILED_DENSE,
  legendItems,
  stepSegments,
  type TimelineRow,
  type TimelineRows,
  timelineRows,
  versusParent,
} from './timeline-rows.js';

/**
 * The Timeline rows, attempt lines and step segments over generated ideas: the order is the Ideas
 * list's, every segment lies on its row's bar, the folded lane stands for exactly the failed
 * ideas in range, and a shorter range never shows an idea that ended before it.
 */

const OUTCOMES: readonly CandidateOutcome[] = ['running', 'passed', 'failed'];

function randomIdeas(random: Seeded): Candidate[] {
  const count = random.int(71);
  return Array.from({length: count}, (_, at) =>
    idea(`i${at}`, OUTCOMES[random.int(3)] as CandidateOutcome, at * 600, 100 + random.int(900)),
  );
}

function checkOrder(rows: readonly TimelineRow[]): void {
  const live = rows.map(row => row.outcome === 'live');
  expect(live).toEqual([...live].sort((a, b) => Number(b) - Number(a)));
  const starts = rows.filter(row => row.outcome !== 'live').map(row => row.start);
  expect(starts).toEqual([...starts].sort((a, b) => b - a));
}

function checkBars(shown: TimelineRows): void {
  for (const row of shown.rows) {
    expect(row.end).toBeGreaterThanOrEqual(row.start);
    for (const segment of row.segments) {
      expect(segment.start).toBeGreaterThanOrEqual(row.start - 1e-9);
      expect(segment.end).toBeLessThanOrEqual(row.end + 1e-9);
    }
    if (!row.lane) expect(row.end >= shown.view.from && row.start <= shown.view.to).toBe(true);
  }
}

function checkLane(shown: TimelineRows): void {
  const rows = shown.rows.filter(row => !row.lane);
  expect(shown.rows.length - rows.length).toBe(shown.folded > 0 ? 1 : 0);
  expect(shown.folded === 0 || shown.folded > FAILED_DENSE).toBe(true);
  expect(shown.folded + rows.filter(row => row.outcome === 'err').length).toBe(shown.failedInRange);
}

describe('timeline rows', () => {
  test('over any run: running first then newest first, segments inside bars, lane counts the failed', () => {
    for (const seed of SEEDS) {
      const ideas = randomIdeas(new Seeded(seed));
      const clock = {start: 0, now: ideas.length * 600 + 1000};
      for (const range of ['hour', 'day', 'all'] as const) {
        const shown = timelineRows(modelOf(ideas), range, clock, false);
        checkOrder(shown.rows.filter(row => !row.lane));
        checkBars(shown);
        checkLane(shown);
        expect(timelineRows(modelOf(ideas), range, clock, true).folded).toBe(0);
      }
    }
  });

  test('a range shows a subset of the ideas the longer range shows', () => {
    const ideas = Array.from({length: 30}, (_, at) => idea(`i${at}`, 'passed', at * 7200, 600));
    const clock = {start: 0, now: 30 * 7200};
    const ids = (range: 'hour' | 'day' | 'all') =>
      timelineRows(modelOf(ideas), range, clock, true).rows.map(row => row.id);
    expect(ids('hour').length).toBeLessThan(ids('day').length);
    expect(ids('day').length).toBeLessThan(ids('all').length);
    expect(ids('all').length).toBe(30);
    for (const id of ids('hour')) expect(ids('day')).toContain(id);
  });

  test('the figure is the ratio of a passed idea and empty while running', () => {
    const shown = timelineRows(
      modelOf([idea('a', 'passed', 0, 100), idea('b', 'running', 200, 100)]),
      'all',
      {start: 0, now: 400},
      false,
    );
    expect(shown.rows.map(row => [row.id, row.figure])).toEqual([
      ['b', ''],
      ['a', '1.50×'],
    ]);
  });
});

describe('the timeline legend', () => {
  test('over any run names exactly the roles drawn on the rows in view, once each, in the colors the bars use', () => {
    for (const seed of SEEDS) {
      const ideas = randomIdeas(new Seeded(seed));
      const clock = {start: 0, now: ideas.length * 600 + 1000};
      for (const range of ['hour', 'all'] as const) {
        const shown = timelineRows(modelOf(ideas), range, clock, false);
        const drawn = new Set(
          shown.rows
            .filter(row => !row.lane)
            .flatMap(row =>
              row.segments.flatMap(segment => (segment.role === null ? [] : [segment.role])),
            ),
        );
        expect(shown.legend.roles.map(entry => entry.role).sort()).toEqual([...drawn].sort());
        for (const {role, series} of shown.legend.roles) expect(series).toBe(roleSeries(role));
        const hatched = shown.rows.some(
          row => !row.lane && row.segments.some(each => each.kind === 'hatched'),
        );
        expect(shown.legend.gates).toBe(hatched);
        const paused = shown.rows.some(
          row => !row.lane && row.segments.some(each => each.kind === 'grey'),
        );
        expect(shown.legend.pauses).toBe(paused);
        expect(legendItems(shown.legend).some(entry => entry.label === 'Failed ideas')).toBe(
          shown.rows.some(row => row.lane),
        );
      }
    }
  });

  test('says nothing for an idea with no steps', () => {
    const bare = idea('a', 'passed', 0, 100);
    const none = {...bare, attempts: [{...(bare.attempts[0] as CandidateAttempt), steps: []}]};
    const shown = timelineRows(modelOf([none]), 'all', {start: 0, now: 200}, false);
    expect(shown.legend).toEqual({roles: [], gates: false, pauses: false});
  });

  test('names the roles a run used: implementer and judge here, and a gate', () => {
    const shown = timelineRows(
      modelOf([idea('a', 'passed', 0, 100)]),
      'all',
      {start: 0, now: 200},
      false,
    );
    expect(shown.legend.roles.map(entry => entry.role)).toEqual(['implementer', 'judge']);
    expect(shown.legend.gates).toBe(true);
  });
});

describe('step segments', () => {
  test('an agent turn is solid in its role color, a gate hatched, and a running step reaches now', () => {
    const steps: CandidateStep[] = [
      step('t', 'agent_turn', 'implementer', 0, 10),
      step('g', 'gate', null, 10, 10),
      step('j', 'agent_turn', 'judge', 20, 10),
      {...step('r', 'agent_turn', 'implementer', 30, 0), endedAt: null, status: 'running'},
    ];
    const segments = stepSegments(steps, 50);
    expect(segments.map(segment => segment.kind)).toEqual(['solid', 'hatched', 'solid', 'solid']);
    expect(segments.map(segment => segment.series)).toEqual([
      roleSeries('implementer'),
      undefined,
      roleSeries('judge'),
      roleSeries('implementer'),
    ]);
    expect(segments[0]?.series).not.toBe(segments[2]?.series);
    expect(segments[3]?.end).toBe(50);
    expect(segments[0]?.label).toBe('Builder · agent_turn t · 10s');
  });
});

describe('attempts', () => {
  test('lines read failed, the ratio or running, in the order the attempts began', () => {
    const base = idea('x', 'passed', 0, 300);
    const failed = attempt('f', 'failed', 0, 60);
    const kept = attempt('k', 'passed', 60, 120);
    const live = attempt('l', 'running', 200, 0);
    const lines = attemptLines({...base, attempts: [failed, kept, live]}, 260, 'ops/s');
    expect(lines.map(line => [line.title, line.result, line.tone])).toEqual([
      ['attempt 1', 'Failed', 'err'],
      ['attempt 2', '1.50×', 'ok'],
      ['attempt 3', 'Running', 'live'],
    ]);
    expect(lines[1]?.value).toBe('40 ops/s');
    expect(lines.map(line => line.took)).toEqual(['1m', '2m', '1m']);
  });

  test('the strip spans the attempt and carries its steps', () => {
    const base = idea('x', 'passed', 100, 300);
    const strip = attemptStrip(base, base.attempts[0] as CandidateAttempt, null);
    expect([strip.from, strip.to, strip.playhead]).toEqual([100, 400, null]);
    expect(strip.row.segments.length).toBe(3);
  });

  test('versus parent follows the metric direction', () => {
    const kept = idea('a', 'passed', 0, 10);
    const measured = kept.measurement;
    if (measured === null) throw new Error('fixture');
    const parent = {...idea('p', 'passed', 0, 10), measurement: {...measured, metricValue: 20}};
    expect(versusParent(kept, parent, 'max')).toBe('2.00×');
    expect(versusParent(kept, parent, 'min')).toBe('0.50×');
    expect(versusParent(kept, null, 'max')).toBeNull();
  });
});
