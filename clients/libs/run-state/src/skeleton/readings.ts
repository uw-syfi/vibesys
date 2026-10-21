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

export function measuredSpans(spans: readonly Span[], view: StoreView): Span[] {
  const bySpan = readingsBySpan(view);
  const input = spans
    .filter(
      span =>
        span.label?.kind === 'gate' &&
        (JSON.parse(span.label.attrs_json ?? '{}') as Record<string, unknown>)['subject'] ===
          'GATE_SUBJECT_INPUT',
    )
    .at(-1);
  const inputReadings = input ? (bySpan.get(input.id) ?? []) : [];
  const metric = view.epochs.at(-1)?.epoch?.run?.objectives[0]?.series;
  const inputValue = inputReadings.find(item => item.name === metric)?.value;
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
