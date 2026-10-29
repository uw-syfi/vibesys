/** Declared metric points join existing measurements without strategy decisions. */
import type {Span} from '@vibesys/contracts';
import {Direction} from '@vibesys/proto';
import type {StoreView} from './types.js';

interface Reading {
  name: string;
  value: number;
  unit: string;
  direction: 'max' | 'min';
}
function readingsBySpan(view: StoreView): ReadonlyMap<string, readonly Reading[]> {
  const points = new Map<string, Map<string, Reading>>();
  for (const history of view.metrics)
    for (const version of history.points) {
      const point = version.point;
      if (!point?.span) continue;
      const epoch = view.epochs.filter(item => item.seq <= version.seq).at(-1)?.epoch;
      const declaration = epoch?.series.find(item => item.name === history.series);
      if (
        !declaration ||
        (declaration.direction !== Direction.MAX && declaration.direction !== Direction.MIN)
      )
        continue;
      const rows = points.get(point.span) ?? new Map<string, Reading>();
      rows.set(history.series, {
        name: history.series,
        value: point.value,
        unit: declaration.unit,
        direction: declaration.direction === Direction.MAX ? 'max' : 'min',
      });
      points.set(point.span, rows);
    }
  return new Map([...points].map(([span, rows]) => [span, [...rows.values()]]));
}

interface InputFact {
  /** The span that measured the input, when the run states it. */
  readonly span?: string;
  readonly name: string;
  readonly value: number;
  readonly unit: string;
  readonly direction: 'max' | 'min';
}

/** The input fact is supplied by the store reader, including retained data upgrades. */
function inputFact(view: StoreView): InputFact | null {
  const epoch = view.epochs.at(-1)?.epoch;
  const objective = epoch?.run?.objectives[0];
  if (!epoch || !objective) return null;
  const stated = epoch.run?.inputMeasurement?.state;
  const value = stated?.case === 'measured' ? stated.value.value : undefined;
  if (value === undefined) return null;
  const declaration = epoch.series.find(item => item.name === objective.series);
  return {
    ...(stated?.case === 'measured' ? {span: stated.value.span} : {}),
    name: objective.series,
    value,
    unit: declaration?.unit ?? '',
    direction: objective.direction === Direction.MIN ? 'min' : 'max',
  };
}

/** The input's measured value of the objective metric, or null before it is measured. */
export function inputMeasurement(view: StoreView): number | null {
  return inputFact(view)?.value ?? null;
}

export function measuredSpans(spans: readonly Span[], view: StoreView): Span[] {
  const bySpan = readingsBySpan(view);
  const input = inputFact(view);
  const metric = input?.name ?? view.epochs.at(-1)?.epoch?.run?.objectives[0]?.series;
  // The span the fact names also holds the input's reading of every other declared objective.
  const inputReadings = input
    ? [
        {name: input.name, value: input.value, unit: input.unit, direction: input.direction},
        ...(input.span === undefined ? [] : (bySpan.get(input.span) ?? [])).filter(
          reading => reading.name !== input.name,
        ),
      ]
    : [];
  const inputValue = input?.value;
  return spans.map(span => {
    if (span.label?.kind !== 'gate') return span;
    const rows = bySpan.get(span.id) ?? [];
    const value = rows.find(item => item.name === metric)?.value;
    const attrs = JSON.parse(span.label.attrs_json ?? '{}') as Record<string, unknown>;
    return {
      ...span,
      label: {
        ...span.label,
        attrs_json: JSON.stringify({
          ...attrs,
          stage:
            typeof attrs['stage'] === 'string'
              ? attrs['stage'].replace('GATE_STAGE_', '').toLowerCase()
              : undefined,
          metrics: rows,
          input_metrics: inputReadings,
          metric_value: value,
          vs_baseline:
            value !== undefined && inputValue !== undefined && inputValue !== 0
              ? value / inputValue
              : undefined,
        }),
      },
    };
  });
}
