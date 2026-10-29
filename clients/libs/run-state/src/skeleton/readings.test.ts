/** The input's value is the run's stated fact: no order or count of gates changes it. */
import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import type {Span} from '@vibesys/contracts';
import {
  Direction,
  EpochStartedSchema,
  EpochVersionSchema,
  InputMeasurementSchema,
  MeasuredInputSchema,
  MetricHistorySchema,
  MetricPointSchema,
  MetricSeriesDeclSchema,
  MetricVersionSchema,
  ObjectiveSchema,
  RunDescriptionSchema,
  UnmeasuredInputSchema,
} from '@vibesys/proto';
import {inputMeasurement, measuredSpans} from './readings.js';
import type {StoreView} from './types.js';

type Stated = 'absent' | 'unmeasured' | number;

const gate = (id: string, subject: string): Span =>
  ({
    id,
    label: {kind: 'gate', attrs_json: JSON.stringify({subject, stage: 'GATE_STAGE_BENCHMARK'})},
  }) as unknown as Span;

function view(measured: Record<string, number>, stated: Stated): StoreView {
  const inputMeasurementField =
    stated === 'absent'
      ? {}
      : {
          inputMeasurement: create(InputMeasurementSchema, {
            state:
              stated === 'unmeasured'
                ? {case: 'unmeasured', value: create(UnmeasuredInputSchema, {})}
                : {
                    case: 'measured',
                    value: create(MeasuredInputSchema, {
                      series: 'speed',
                      value: stated,
                      span: 'gate:input:0',
                    }),
                  },
          }),
        };
  const epoch = create(EpochStartedSchema, {
    run: create(RunDescriptionSchema, {
      objectives: [create(ObjectiveSchema, {series: 'speed', direction: Direction.MAX})],
      ...inputMeasurementField,
    }),
    series: [
      create(MetricSeriesDeclSchema, {name: 'speed', unit: 'ops', direction: Direction.MAX}),
    ],
  });
  return {
    seq: 100n,
    spans: {},
    lifecycle: undefined,
    epochs: [create(EpochVersionSchema, {seq: 1n, epoch})],
    metrics: [
      create(MetricHistorySchema, {
        series: 'speed',
        points: Object.entries(measured).map(([span, value], at) =>
          create(MetricVersionSchema, {
            seq: BigInt(2 + at),
            point: create(MetricPointSchema, {series: 'speed', span, value, clock: at}),
          }),
        ),
      }),
    ],
  };
}

/** Every ordering of `items`. */
function orderings<T>(items: readonly T[]): T[][] {
  if (items.length <= 1) return [[...items]];
  return items.flatMap((item, at) =>
    orderings([...items.slice(0, at), ...items.slice(at + 1)]).map(rest => [item, ...rest]),
  );
}

test('every surface shows the stated input, whatever the gates and their order', () => {
  const candidate = 'gate:vs-a:benchmark';
  for (const count of [0, 1, 2, 3]) {
    const gates = Array.from({length: count}, (_, at) => `gate:input:${at}`);
    for (const order of orderings([...gates, candidate])) {
      const spans = order.map(id =>
        gate(id, id === candidate ? 'GATE_SUBJECT_CANDIDATE' : 'GATE_SUBJECT_INPUT'),
      );
      // Readings sit on input gates that the fact does not name; none of them is the input.
      const readings = {...Object.fromEntries(gates.map(id => [id, 7])), [candidate]: 250};
      const stated = view(readings, 100);
      const row = measuredSpans(spans, stated).find(span => span.id === candidate);
      expect(JSON.parse(row?.label?.attrs_json ?? '{}').vs_baseline).toBe(2.5);
      expect(inputMeasurement(stated)).toBe(100);
    }
  }
});

test('an absent or unmeasured input has no ratio', () => {
  const spans = [gate('gate:input:0', 'GATE_SUBJECT_INPUT'), gate('c', 'GATE_SUBJECT_CANDIDATE')];
  const unmeasured = view({c: 250}, 'unmeasured');
  expect(inputMeasurement(unmeasured)).toBeNull();
  const row = measuredSpans(spans, unmeasured).find(span => span.id === 'c');
  expect(JSON.parse(row?.label?.attrs_json ?? '{}').vs_baseline).toBeUndefined();
  const older = view({c: 250}, 'absent');
  expect(inputMeasurement(older)).toBeNull();
  const older2 = measuredSpans(spans, older).find(span => span.id === 'c');
  expect(JSON.parse(older2?.label?.attrs_json ?? '{}').vs_baseline).toBeUndefined();
});

test("the input span the fact names also gives the input's reading of every other objective", () => {
  const base = view({'gate:input:0': 100, 'gate:input:1': 90, c: 250}, 100);
  const epoch = base.epochs[0]?.epoch;
  if (!epoch) throw new Error('epoch required');
  epoch.series.push(
    create(MetricSeriesDeclSchema, {name: 'latency', unit: 'ms', direction: Direction.MIN}),
  );
  const latency = (span: string, value: number, at: number) =>
    create(MetricVersionSchema, {
      seq: BigInt(20 + at),
      point: create(MetricPointSchema, {series: 'latency', span, value, clock: at}),
    });
  const withLatency: StoreView = {
    ...base,
    metrics: [
      ...base.metrics,
      create(MetricHistorySchema, {
        series: 'latency',
        // Another input gate reads another latency; only the named span's is the input's.
        points: [latency('gate:input:0', 12.4, 0), latency('gate:input:1', 99, 1)],
      }),
    ],
  };
  const row = measuredSpans([gate('c', 'GATE_SUBJECT_CANDIDATE')], withLatency)[0];
  const inputs = JSON.parse(row?.label?.attrs_json ?? '{}').input_metrics;
  expect(inputs).toEqual([
    {name: 'speed', value: 100, unit: 'ops', direction: 'max'},
    {name: 'latency', value: 12.4, unit: 'ms', direction: 'min'},
  ]);
});
