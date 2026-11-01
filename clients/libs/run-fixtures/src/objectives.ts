/**
 * The two-objective run read as a run of three or six: the same ideas and spans, each gate reading
 * the extra objectives too, so a screen drawn for any number of objectives has a run to be drawn
 * from. The extra readings follow the run's own (a latency or a throughput) by a fixed rule, so the
 * fixture is the same on every machine; one of the six never moves from its input. Not a committed
 * scenario: it carries no native golden, the harness projects it as it does any variant.
 */
import {fixtureNamed} from './catalog.js';
import {parseFixture, type RunFixture} from './fixture.js';

/** The readings a gate states of the base run, from which the extra objectives are derived. */
interface Base {
  readonly ops: number;
  readonly latency: number;
}

interface Extra {
  readonly name: string;
  readonly unit: string;
  readonly direction: 'max' | 'min';
  readonly of: (base: Base, input: Base) => number;
}

const round = (value: number, places: number): number => {
  const scale = 10 ** places;
  return Math.round(value * scale) / scale;
};

/** In order: the third objective of three, then the fourth to sixth of six. */
const EXTRAS: readonly Extra[] = [
  {name: 'ttft_ms', unit: 'ms', direction: 'min', of: base => round(base.latency * 0.42, 1)},
  {
    name: 'memory_mb',
    unit: 'MB',
    direction: 'min',
    of: base => Math.round(900 + 60 * base.latency),
  },
  // Never moves: every reading is the input's.
  {name: 'completed_requests', unit: '', direction: 'max', of: () => 2},
  {
    name: 'cost_usd',
    unit: '$',
    direction: 'min',
    of: (base, input) => round(3 + 12 * Math.sqrt(input.ops / base.ops), 2),
  },
];

interface Metric {
  name: string;
  value: number;
  unit: string;
  direction: string;
}

interface Attrs {
  metrics?: Metric[];
  input_metrics?: Metric[];
  [key: string]: unknown;
}

function baseOf(metrics: readonly Metric[]): Base | null {
  const ops = metrics.find(each => each.name === 'total_ops_per_sec');
  const latency = metrics.find(each => each.name === 'p99_latency');
  return ops === undefined || latency === undefined
    ? null
    : {ops: ops.value, latency: latency.value};
}

const metricsOf = (extras: readonly Extra[], base: Base, input: Base): Metric[] =>
  extras.map(each => ({
    name: each.name,
    value: each.of(base, input),
    unit: each.unit,
    direction: each.direction,
  }));

interface Span {
  label?: {attrs_json?: string};
}

/** `attrs` with the extra objectives read beside the base run's two. */
function widen(attrs: Attrs, extras: readonly Extra[], input: Base): Attrs {
  const base = baseOf(attrs.metrics ?? []);
  const own = attrs.input_metrics === undefined ? null : baseOf(attrs.input_metrics);
  return {
    ...attrs,
    ...(base === null || attrs.metrics === undefined
      ? {}
      : {metrics: [...attrs.metrics, ...metricsOf(extras, base, input)]}),
    ...(own === null || attrs.input_metrics === undefined
      ? {}
      : {input_metrics: [...attrs.input_metrics, ...metricsOf(extras, own, input)]}),
  };
}

/** The input's own readings: the first gate that states metrics and no input before it. */
function inputOf(events: readonly {data: {span?: Span} | null}[]): Base {
  for (const event of events) {
    const text = event.data?.span?.label?.attrs_json;
    const found = text === undefined ? null : baseOf((JSON.parse(text) as Attrs).metrics ?? []);
    if (found !== null) return found;
  }
  throw new Error('the run states no reading of its input');
}

/**
 * `base` (a run of two objectives, throughput and latency) as a run of `count` objectives, three or
 * six, named `name`.
 */
function withObjectives(base: RunFixture, count: 3 | 6, name: string): RunFixture {
  const extras = EXTRAS.slice(0, count === 3 ? 1 : 4);
  const events = JSON.parse(JSON.stringify(base.events)) as {
    data: {
      span?: Span;
      overview?: {objective: {objectives: {name: string; direction: string}[]}};
    } | null;
  }[];
  const input = inputOf(events);
  for (const event of events) {
    const objectives = event.data?.overview?.objective.objectives;
    if (objectives !== undefined) {
      objectives.push(...extras.map(each => ({name: each.name, direction: each.direction})));
    }
    const label = event.data?.span?.label;
    if (label?.attrs_json === undefined) continue;
    label.attrs_json = JSON.stringify(widen(JSON.parse(label.attrs_json) as Attrs, extras, input));
  }
  return parseFixture({
    ...base,
    name,
    description: `${base.description} Read as a run of ${count} objectives.`,
    events,
  });
}

let three: RunFixture | undefined;
let six: RunFixture | undefined;

/** A finished run of three objectives (the third follows the latency) or of six (one never moves). */
export function objectivesRun(count: 3 | 6): RunFixture {
  const base = fixtureNamed('finished-two-objectives');
  if (count === 3) {
    three ??= withObjectives(base, 3, 'finished-three-objectives');
    return three;
  }
  six ??= withObjectives(base, 6, 'finished-six-objectives');
  return six;
}
