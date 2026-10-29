import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunOverview, Span} from '@vibesys/contracts';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {traceLog} from './live.js';
import {clockStart, heroView, nowView, stripChartView} from './run-level.js';
import {createRunReader, type RunModel} from './run-model.js';

/**
 * A multi-agent (serial) run, as its span events arrive: the input's measurement, then one round
 * after another, each a workstream that is labelled `round N` as it begins, named for its
 * hypothesis once the designer chose one, and passed or failed as its workspace settles. Each
 * round's designer, implementer and judge turns and its gate are its own operations. The spans are
 * those the multi search's fake-core story records (`tests/vibesys/serial_search/
 * test_span_labels.py`), with times. Regression: before the serial searches labelled their spans,
 * this run's Overview said no candidate had passed, 0 workstreams and nothing measured; the run
 * level reads its best, its chart, its ideas and what runs now from the same spans.
 */

const OVERVIEW: RunOverview = {
  overview_version: 1,
  objective: {text: 'Maximize throughput.', metric: 'throughput', unit: 'ops/s', direction: 'max'},
  benchmarks: {},
};

type Label = NonNullable<Span['label']>;

/** Writes span events as the runtime does: each carries the whole span as it now is. */
class Recording {
  readonly events: RunEvent[] = [];
  readonly #spans = new Map<string, Span>();
  #clock = 0;

  start(id: string, parent: string | null, name: string, label?: Label): void {
    const type = id.startsWith('op:') ? 'operation' : 'flow';
    this.#clock += 10;
    const span: Span = {id, parent, type, name, status: 'running', started_at: this.#clock};
    this.#emit('span_started', label === undefined ? span : {...span, label});
  }

  relabel(id: string, label: Partial<Label>): void {
    const span = this.#spans.get(id) as Span;
    this.#emit('span_updated', {...span, label: {...(span.label as Label), ...label}});
  }

  end(id: string, label?: Partial<Label>): void {
    const span = this.#spans.get(id) as Span;
    this.#clock += 10;
    const ended: Span = {...span, status: 'done', ended_at: this.#clock};
    this.#emit(
      'span_ended',
      label === undefined ? ended : {...ended, label: {...(span.label as Label), ...label}},
    );
  }

  #emit(type: string, span: Span): void {
    this.#spans.set(span.id, span);
    this.events.push({
      protocol_version: 1,
      run_id: 'run',
      sequence: this.events.length + 1,
      timestamp: '2026-10-16T12:00:00Z',
      type,
      data: {kind: type, span},
    } as RunEvent);
  }
}

/** A round's attributes once it settled: its base and the code it kept. */
const settled = (n: number, builds?: string): string =>
  JSON.stringify({
    hypothesis_id: `h${n}`,
    parent_revision: builds === undefined ? 'base' : `c${n - 1}`,
    base_round: builds === undefined ? null : n - 1,
    round: n,
    revision: `c${n}`,
  });

/** The search's adoption span: its current best (a revision), and the verdict once it ended. */
function adopt(run: Recording, candidate: string, value: number, vs: number): void {
  run.relabel('op:adopt', {
    attrs_json: JSON.stringify({candidate, metric_value: value, vs_baseline: vs}),
  });
}

const turn = (role: string): Label => ({
  kind: 'agent_turn',
  title: role,
  attrs_json: JSON.stringify({role, model: null}),
});

const gate = (): Label => ({
  kind: 'gate',
  title: 'accuracy + benchmark',
  attrs_json: JSON.stringify({stage: 'accuracy,benchmark'}),
});

/** One round: designed, implemented, judged, measured and settled; returns its span id. */
function round(
  run: Recording,
  n: number,
  measured: {value: number; vs: number; verdict: string},
  builds?: string,
): string {
  const id = `flow:00000${n}/#1`;
  const hypothesis = `h${n}`;
  run.start(id, 'flow:#1', 'play_round', {
    kind: 'workstream',
    title: `round ${n}`,
    attrs_json: JSON.stringify({hypothesis_id: null, parent_revision: null, round: n}),
  });
  run.start(`op:00000${n}/orchestrator.1`, id, 'orchestrator', turn('orchestrator'));
  run.end(`op:00000${n}/orchestrator.1`);
  run.relabel(id, {
    title: `title ${hypothesis}`,
    attrs_json: JSON.stringify({
      hypothesis_id: hypothesis,
      parent_revision: builds === undefined ? 'base' : `c${n - 1}`,
      base_round: builds === undefined ? null : n - 1,
      round: n,
    }),
    links: builds === undefined ? [] : [{rel: 'builds_on', to: builds}],
  });
  for (const role of ['implementer', 'judge']) {
    run.start(`op:00000${n}/${role}.1`, id, role, turn(role));
    run.end(`op:00000${n}/${role}.1`);
  }
  run.start(`op:00000${n}/measure.1`, id, 'measure', gate());
  run.end(`op:00000${n}/measure.1`, {
    verdict: measured.verdict,
    attrs_json: JSON.stringify({
      stage: 'accuracy,benchmark',
      metric_value: measured.value,
      vs_baseline: measured.vs,
    }),
  });
  run.end(id, {verdict: 'passed', attrs_json: settled(n, builds)});
  return id;
}

function recorded(): Recording {
  const run = new Recording();
  run.start('flow:#1', null, 'root');
  run.start('op:measure.1', 'flow:#1', 'measure');
  run.end('op:measure.1');
  run.start('op:adopt', 'flow:#1', 'adopt', {
    kind: 'adoption',
    title: 'the input',
    attrs_json: JSON.stringify({candidate: null, metric_value: 50, vs_baseline: 1}),
  });
  const first = round(run, 1, {value: 100, vs: 2, verdict: 'passed'});
  adopt(run, 'c1', 100, 2);
  // a round slower than the input still passes, and is not the best
  round(run, 2, {value: 10, vs: 0.2, verdict: 'slower'}, first);
  return run;
}

/** The run the screen reads after the first `sequence` events. */
function at(events: readonly RunEvent[], sequence = events.length): RunModel {
  return createRunReader().read(traceLog(EMPTY_TRACE, OVERVIEW, events.slice(0, sequence)), null);
}

describe('scrubbing a multi-agent run', () => {
  test('the chart is the whole run at every scrub position: axis fixed, cursor moves, later points dim', () => {
    const {events} = recorded();
    const whole = at(events);
    const start = clockStart(whole);
    const fixed = stripChartView(whole, start, null);
    expect(fixed).not.toBeNull();
    for (let sequence = 1; sequence <= events.length; sequence += 1) {
      const viewed = at(events, sequence);
      const scrub = viewed.latest ?? start;
      const chart = stripChartView(whole, start, null, scrub);
      // Rendered (never null, even before the first measurement) with the same axes.
      expect(chart).not.toBeNull();
      expect(chart?.yDomain).toEqual(fixed?.yDomain as readonly [number, number]);
      expect(chart?.yTicks).toEqual(fixed?.yTicks as never);
      expect(chart?.xTicks).toEqual(fixed?.xTicks as never);
      expect(chart?.points.map(point => point.id)).toEqual(
        fixed?.points.map(point => point.id) as string[],
      );
      expect(chart?.playhead).toBe((scrub - start) / 60);
      for (const point of chart?.points ?? [])
        expect(point.faint).toBe(point.x * 60 + start > scrub);
    }
    expect(fixed?.yDomain[0]).toBe(0);
    expect(fixed?.yDomain[1]).toBeGreaterThan(100);
  });
});

describe('a multi-agent run', () => {
  test('regression: the run level shows the best idea, its chart and both rounds', () => {
    const model = at(recorded().events);
    expect(heroView(model)).toMatchObject({measured: 2});
    expect(heroView(model).tiles[0]).toMatchObject({
      ratio: '2.00×',
      note: 'faster than input',
      detail: '100 ops/s · input 50 ops/s',
    });
    const chart = stripChartView(model, clockStart(model), null);
    expect(chart?.points.map(point => point.y)).toEqual([100, 10]);
    expect(chart?.reference?.y).toBe(50);
  });

  test('the ideas list each round by its hypothesis, with its ratio and what it builds on', () => {
    const {rows} = ideasView(at(recorded().events), IDEAS_FILTER_DEFAULT);
    expect(
      rows.map(row => [row.title, row.figure, row.trail.label, row.state.label]).sort(),
    ).toEqual([
      ['title h1', '2.00×', 'input', 'Passed'],
      ['title h2', '0.20×', 'best', 'Passed'],
    ]);
    expect(rows.map(row => [row.title, row.badges.map(badge => badge.label)]).sort()).toEqual([
      ['title h1', ['best']],
      ['title h2', []],
    ]);
  });

  test('the hero follows the adoption span as a better round lands, then calls it adopted', () => {
    const run = recorded();
    const better = round(run, 3, {value: 300, vs: 6, verdict: 'passed'}, 'flow:000001/#1');
    adopt(run, 'c3', 300, 6);
    expect(better).toBe('flow:000003/#1');
    expect(heroView(at(run.events)).tiles[0]?.ratio).toBe('6.00×');
    run.end('op:adopt', {verdict: 'adopted'});
    const model = at(run.events);
    expect(heroView(model).tiles[0]?.ratio).toBe('6.00×');
    const rows = ideasView(model, IDEAS_FILTER_DEFAULT).rows;
    expect(rows.filter(row => row.badges.some(badge => badge.label === 'adopted'))).toHaveLength(1);
    expect(rows.some(row => row.badges.some(badge => badge.label === 'best'))).toBe(false);
  });

  test('a round choosing its hypothesis reads as its orchestrator running now', () => {
    const {events} = recorded();
    const designing = events.findIndex(
      event =>
        event.type === 'span_started' &&
        (event.data as {span: Span}).span.id === 'op:000002/orchestrator.1',
    );
    expect(nowView(at(events, designing + 1)).map(item => [item.context, item.doing])).toEqual([
      ['round 2', 'Planner'],
    ]);
  });
});

/**
 * The order a live multi run emits its spans (`tests/vibesys/serial_search/test_span_labels.py`
 * plays it): the round's gate is labelled as the measurement is issued, the measurement ends, and
 * only once its evidence is read does the gate's label gain the value, the ratio and the verdict
 * (a `span_updated` of a span that already ended); then the round settles accepted and the next
 * round begins designing.
 */
function liveRoundOne(run: Recording): void {
  run.start('flow:#1', null, 'root');
  run.start('op:measure.1', 'flow:#1', 'measure');
  run.end('op:measure.1');
  const id = 'flow:000001/#1';
  run.start(id, 'flow:#1', 'play_round', {
    kind: 'workstream',
    title: 'round 1',
    attrs_json: JSON.stringify({hypothesis_id: null, parent_revision: null, round: 1}),
  });
  run.relabel(id, {
    title: 'title h1',
    attrs_json: JSON.stringify({hypothesis_id: 'h1', parent_revision: 'base', round: 1}),
  });
  run.start('op:adopt', 'flow:#1', 'adopt', {
    kind: 'adoption',
    title: 'the input',
    attrs_json: JSON.stringify({candidate: null, metric_value: 50, vs_baseline: 1}),
  });
  run.start('op:000001/measure.1', id, 'measure', gate());
  run.end('op:000001/measure.1');
  run.relabel('op:000001/measure.1', {
    verdict: 'passed',
    attrs_json: JSON.stringify({stage: 'accuracy,benchmark', metric_value: 100, vs_baseline: 2}),
  });
  run.end(id, {verdict: 'passed', attrs_json: settled(1)});
  adopt(run, 'c1', 100, 2);
}

describe('a live multi run whose round 1 passed', () => {
  test('mid-run the hero shows the best idea and the chart its point', () => {
    const run = new Recording();
    liveRoundOne(run);
    run.start('flow:000002/#1', 'flow:#1', 'play_round', {
      kind: 'workstream',
      title: 'round 2',
      attrs_json: JSON.stringify({hypothesis_id: null, parent_revision: null, round: 2}),
    });
    const model = at(run.events);
    expect(heroView(model).tiles[0]).toMatchObject({
      ratio: '2.00×',
      detail: expect.stringContaining('100 ops/s'),
    });
    const chart = stripChartView(model, clockStart(model), null);
    expect(chart?.points.map(point => point.y)).toEqual([100]);
  });

  test('once the run finished nothing runs now, and the best stays', () => {
    const run = new Recording();
    liveRoundOne(run);
    run.end('op:adopt', {verdict: 'adopted'});
    run.end('flow:#1');
    const model = at(run.events);
    expect(nowView(model)).toEqual([]);
    expect(heroView(model).tiles[0]?.ratio).toBe('2.00×');
  });
});

/** A dynamic run's story: rounds pass one after another while no adoption span names the best. */
function dynamicRun(adoption: 'none' | 'input'): {run: Recording; roundsEnd: number[]} {
  const run = new Recording();
  run.start('flow:#1', null, 'root');
  run.start('op:measure.1', 'flow:#1', 'measure');
  run.end('op:measure.1');
  if (adoption === 'input') {
    // the search labelled its adoption with the input, and has not named a leader since
    run.start('op:adopt', 'flow:#1', 'adopt', {
      kind: 'adoption',
      title: 'the input',
      attrs_json: JSON.stringify({candidate: null, metric_value: 50, vs_baseline: 1}),
    });
  }
  const roundsEnd: number[] = [];
  let first: string | undefined;
  for (const [n, value] of [100, 80, 300].entries()) {
    const id = round(run, n + 1, {value, vs: value / 50, verdict: 'passed'}, first);
    first ??= id;
    roundsEnd.push(run.events.length);
  }
  return {run, roundsEnd};
}

describe('the best idea while the run works', () => {
  for (const adoption of ['none', 'input'] as const) {
    test(`is the best passed idea so far at every event, with no leader named (${adoption})`, () => {
      const {run, roundsEnd} = dynamicRun(adoption);
      const values = [100, 80, 300];
      for (let sequence = 1; sequence <= run.events.length; sequence += 1) {
        const model = at(run.events, sequence);
        const settled = roundsEnd.filter(end => end <= sequence).length;
        const hero = heroView(model);
        if (settled === 0) {
          expect(hero.tiles[0]).toMatchObject({ratio: null, note: null});
          expect(hero.tiles[0]?.detail).toContain('nothing measured yet');
          continue;
        }
        const best = Math.max(...values.slice(0, settled));
        expect(hero.tiles[0]).toMatchObject({
          ratio: `${(best / 50).toFixed(2)}×`,
          detail: expect.stringContaining(`${best} ops/s`),
        });
        const badged = ideasView(model, IDEAS_FILTER_DEFAULT).rows.filter(each =>
          each.badges.some(badge => badge.label === 'best'),
        );
        expect(badged.map(each => each.title)).toEqual([`title h${values.indexOf(best) + 1}`]);
      }
    });
  }

  test('becomes adopted when the run ends, and the adoption span names it once it opens', () => {
    const {run} = dynamicRun('none');
    run.start('op:adopt', 'flow:#1', 'adopt', {
      kind: 'adoption',
      title: 'c3',
      attrs_json: JSON.stringify({candidate: 'c3', metric_value: 300, vs_baseline: 6}),
    });
    expect(heroView(at(run.events)).tiles[0]?.ratio).toBe('6.00×');
    run.end('op:adopt', {verdict: 'adopted'});
    const model = at(run.events);
    expect(heroView(model).tiles[0]?.ratio).toBe('6.00×');
    const badges = ideasView(model, IDEAS_FILTER_DEFAULT).rows.flatMap(row =>
      row.badges.map(badge => badge.label),
    );
    expect(badges).toEqual(['adopted']);
  });

  test('is nothing when the run could not adopt anything', () => {
    const {run} = dynamicRun('none');
    run.start('op:adopt', 'flow:#1', 'adopt', {
      kind: 'adoption',
      title: 'c3',
      attrs_json: JSON.stringify({candidate: 'c3'}),
    });
    run.end('op:adopt', {verdict: 'not_adopted'});
    expect(heroView(at(run.events)).tiles[0]?.ratio).toBeNull();
  });
});
