import type {JSX, ReactNode} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/**
 * A row of metric tiles, one per measure, laid out as a dashboard: every tile the same size on one
 * grid, wrapping to the next row when the width is used up. The row is as wide as its tiles
 * (`count`) up to its container's width, so a single tile is one tile wide and a second one
 * widens the row rather than shrinking the first. `stacked` puts the tiles one above the other,
 * one tile wide: for a block that sits beside a chart.
 */
export function MetricTiles(props: {
  readonly label: string;
  readonly count: number;
  readonly stacked?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const style: StyleWithVars = {'--tile-count': String(Math.max(1, props.count))};
  return (
    <section
      className="vs-tiles"
      aria-label={props.label}
      data-stacked={props.stacked === true || undefined}
      style={style}
    >
      {props.children}
    </section>
  );
}

/**
 * One metric: its `name` in words, the headline `figure` large with a short `note` after it (what
 * the figure means: "faster than input"), a quiet `detail` line below (the current value and the
 * reference value, in the same font and size), and a small `trend` at the bottom. The figure is in
 * the `ok` tone when `tone` says it is an improvement, `err` when a regression, else `muted`.
 */
export function MetricTile(props: {
  readonly name: string;
  readonly figure: string;
  readonly note?: string;
  readonly tone?: 'ok' | 'err' | 'muted';
  readonly detail?: string;
  /** The detail's tooltip when it says more than the detail does; else the detail. */
  readonly hint?: string;
  readonly trend?: ReactNode;
}): JSX.Element {
  return (
    <article className="vs-tile">
      <Text variant="small" tone="muted">
        {props.name}
      </Text>
      <div className="vs-tile-figure">
        <Text variant="hero" tone={props.tone ?? 'default'}>
          {props.figure}
        </Text>
        {props.note === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.note}
          </Text>
        )}
      </div>
      <span className="vs-tile-detail" title={props.hint ?? props.detail}>
        <Text variant="small" tone="muted">
          {props.detail ?? ' '}
        </Text>
      </span>
      <div className="vs-tile-trend">{props.trend}</div>
    </article>
  );
}
