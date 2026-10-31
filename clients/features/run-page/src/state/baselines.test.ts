import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {baselineColumns, improvement, overBase} from './baselines.js';
import {EMPTY_TRACE_LOG, traceStep} from './live.js';
import {createFactsReader} from './run-facts.js';
import {heroView} from './headline.js';
import {objectiveCharts} from './objective-chart.js';
import {createRunReader, type RunModel} from './run-model.js';

/**
 * The comparison baselines a run read, as the run page words them: the headline of a run with no
 * reading of its input, the chart's reference lines, the Ideas table's ratio columns and the status
 * words of the comparisons with no reading. Every model is read from events, through the fold and
 * the facts reader the page uses.
 */

type Row = Extract<
  NonNullable<RunEvent['data']>,
  {kind: 'comparisons_read'}
>['comparisons'][number];

/** A `comparisons_read` event as the run of `last` goes on after it. */
function comparisonsRead(last: RunEvent, sequence: number, comparisons: Row[]): RunEvent {
  return {
    ...last,
    sequence,
    type: 'comparisons_read',
    data: {kind: 'comparisons_read', comparisons},
  } as RunEvent;
}

function modelOf(events: readonly RunEvent[]): RunModel {
  const trace = traceStep(EMPTY_TRACE_LOG, {kind: 'events', events});
  const core = reduceEventBatch(initialCoreState(), events);
  const facts = createFactsReader()({core, trace, heard: null});
  return createRunReader().read(trace, null, null, facts);
}

const VLLM: Row = {
  name: 'vllm',
  label: 'vLLM',
  status: 'measured',
  metric: 'total_ops_per_sec',
  unit: 'ops/s',
  direction: 'max',
  value: 66.2,
  spread: 0.02,
  samples: 3,
  hosts: ['host-a'],
};

/** The scenario's run: no reading of its input, vLLM measured, SGLang not measured. */
const SCENARIO = fixtureNamed('comparison-baselines').events;

/** The scenario with its comparison reading replaced by `rows`, the sequence kept. */
function withReading(rows: Row[]): RunEvent[] {
  return SCENARIO.map(event =>
    event.type === 'comparisons_read' ? comparisonsRead(event, event.sequence ?? 0, rows) : event,
  );
}
const WITHOUT_READING = withReading([]);

function onlyTile(model: RunModel) {
  const [tile, ...more] = heroView(model).headlines;
  if (tile === undefined || more.length > 0) throw new Error('expected one tile');
  return tile;
}

describe('the headline of a run with no reading of its input', () => {
  test('reads the best against the measured comparison, worded vs its label', () => {
    const tile = onlyTile(modelOf(SCENARIO));
    expect(tile.ratio).toBe('5.82×');
    expect(tile.figure).toBe('5.82×');
    expect(tile.note).toBe('vs vLLM');
    expect(tile.tone).toBe('ok');
    expect(tile.hint).toContain('5.82× vs vLLM 66.2 ops/s');
    expect(`${tile.note} ${tile.detail} ${tile.hint}`).not.toContain('input');
  });

  test('names every measured comparison, the first in the headline and the rest in the detail', () => {
    const other: Row = {...VLLM, name: 'other', label: 'Other', value: 770.8};
    const events = withReading([VLLM, other]);
    const tile = onlyTile(modelOf(events));
    expect(tile.note).toBe('vs vLLM');
    expect(tile.detail).toContain('0.50× vs Other');
    expect(tile.hint).toContain('0.50× vs Other 770.8 ops/s');
  });

  test('is slower than a comparison the best has not reached', () => {
    const fast: Row = {...VLLM, value: 1000};
    const tile = onlyTile(modelOf(withReading([fast])));
    expect(tile.figure).toBe('0.39×');
    expect(tile.tone).toBe('err');
  });

  test('keeps the value as the headline when no comparison has a reading', () => {
    const none: Row[] = [
      {name: 'a', label: 'A', status: 'failed', reason: 'exited'},
      {name: 'b', label: 'B', status: 'not_configured'},
    ];
    const tile = onlyTile(modelOf(withReading(none)));
    expect(tile.ratio).toBeNull();
    expect(tile.figure).toBe('385.4');
    expect(tile.detail).toBe('1.10× vs round 1');
  });

  test('is unchanged by a comparison once the input has a reading', () => {
    const read = fixtureNamed('finished-run').events;
    const without = heroView(modelOf(read)).headlines;
    const withOne = heroView(
      modelOf([...read, comparisonsRead(read.at(-1) as RunEvent, read.length + 1, [VLLM])]),
    ).headlines;
    expect(withOne).toEqual(without);
    expect(without[0]?.note).toContain('input');
  });
});

describe('the status words of the comparisons with no reading', () => {
  test('each is a small label by its status; a measured one has none', () => {
    const rows: Row[] = [
      VLLM,
      {name: 'a', label: 'A', status: 'failed', reason: 'exited'},
      {name: 'b', label: 'B', status: 'not_configured'},
      {name: 'c', label: 'C', status: 'not_measured'},
    ];
    const hero = heroView(modelOf(withReading(rows)));
    expect(hero.baselineNotes).toEqual([
      {label: 'A', words: 'failed'},
      {label: 'B', words: 'not configured'},
      {label: 'C', words: 'not measured'},
    ]);
  });

  test('a run that read none has none', () => {
    expect(heroView(modelOf(WITHOUT_READING)).baselineNotes).toEqual([]);
  });

  test('the newest reading replaces the earlier ones', () => {
    const events = [
      ...withReading([{name: 'a', label: 'A', status: 'failed'}]),
      comparisonsRead(SCENARIO.at(-1) as RunEvent, SCENARIO.length + 1, [VLLM]),
    ];
    const model = modelOf(events);
    expect(heroView(model).baselineNotes).toEqual([]);
    expect(onlyTile(model).note).toBe('vs vLLM');
  });
});

describe('the strip chart', () => {
  test('draws one reference line per measured comparison, by its label, and none for the rest', () => {
    const chart = stripChartView(modelOf(SCENARIO), 0, null);
    expect(chart?.baselines).toEqual([{y: 66.2, label: 'vLLM'}]);
    const two = [VLLM, {...VLLM, name: 'o', label: 'Other', value: 200}];
    const more = stripChartView(modelOf(withReading(two)), 0, null);
    expect(more?.baselines.map(line => line.label)).toEqual(['vLLM', 'Other']);
  });

  test('draws none when no comparison has a reading, however many were read', () => {
    const rows: Row[] = [
      {name: 'a', label: 'A', status: 'failed'},
      {name: 'b', label: 'B', status: 'not_configured'},
      {name: 'c', label: 'C', status: 'not_measured'},
    ];
    const chart = stripChartView(modelOf(withReading(rows)), 0, null);
    expect(chart?.baselines).toEqual([]);
  });

  test('keeps a line inside the axis, whatever its value', () => {
    const far: Row = {...VLLM, value: 9000};
    const chart = stripChartView(modelOf(withReading([far])), 0, null);
    expect(chart?.yDomain[1]).toBeGreaterThan(9000);
  });

  test('does not draw a comparison of another metric', () => {
    const other: Row = {...VLLM, metric: 'p99_latency'};
    const chart = stripChartView(modelOf(withReading([other])), 0, null);
    expect(chart?.baselines).toEqual([]);
  });
});

describe('the Ideas table columns', () => {
  test('one per comparison, an idea’s value over the comparison’s, a dash when either is missing', () => {
    const columns = baselineColumns(modelOf(SCENARIO));
    expect(columns.labels).toEqual(['vLLM', 'SGLang']);
    expect([...columns.cells.values()]).toEqual([
      ['5.29×', '–'],
      ['5.82×', '–'],
    ]);
  });

  test('an idea with no value has a dash in every column', () => {
    const model = modelOf(SCENARIO);
    const unmeasured: RunModel = {
      ...model,
      candidates: {
        ...model.candidates,
        candidates: model.candidates.candidates.map(item => ({...item, measurement: null})),
      },
    };
    for (const cells of baselineColumns(unmeasured).cells.values()) {
      expect(cells).toEqual(['–', '–']);
    }
  });

  test('a run that read no comparison has no column', () => {
    expect(baselineColumns(modelOf(WITHOUT_READING)).labels).toEqual([]);
  });
});

describe('the ratio to a comparison', () => {
  const values = [0.5, 1, 2, 7.25, 400];
  const bases = [0.001, 1, 66.2, 1000];

  test('is the value over the base where higher is better, and its inverse where lower is', () => {
    for (const value of values) {
      for (const base of bases) {
        const max = overBase(value, base, 'max');
        const min = overBase(value, base, 'min');
        expect(max).toBe(`${(value / base).toFixed(2)}×`);
        expect(min).toBe(`${(base / value).toFixed(2)}×`);
      }
    }
  });

  test('is better than 1 exactly when the value is the better side of the base', () => {
    for (const value of values) {
      for (const base of bases) {
        expect(improvement(value / base, 'max') > 1).toBe(value > base);
        expect(improvement(value / base, 'min') > 1).toBe(value < base);
      }
    }
  });

  test('has none against a base of zero or less', () => {
    for (const base of [0, -1]) expect(overBase(5, base, 'max')).toBeNull();
  });
});
