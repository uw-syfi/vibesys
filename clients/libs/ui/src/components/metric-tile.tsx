import {createContext, type JSX, type ReactNode, useContext} from 'react';
import {SIZE, SPACE} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/** Whether the tiles being drawn are the packed ones (`MetricTiles` `dense`). */
const Dense = createContext(false);

/**
 * How many dense tiles stand in one row of a row `width` pixels wide holding `count` tiles: as many
 * as keep each at its least width (`--column-tile`) with the gap between them, never more than
 * there are. The stylesheet lays the tiles on exactly these tracks.
 */
export function denseTilesPerRow(width: number, count: number): number {
  const least = SIZE['column-tile'];
  const gap = SPACE['space-4'];
  const fit = Math.max(1, Math.floor((width + gap) / (least + gap)));
  return Math.max(1, Math.min(count, fit));
}

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
  /**
   * `chart`: the tiles stand on the same tracks as `ChartColumns` (a tile is as wide as a chart and
   * the gap is a chart's), so tiles above charts share their edges.
   */
  readonly track?: 'chart';
  /**
   * Compact tiles for a page that packs many: the row fills its container with as many tiles as keep
   * their least width (`denseTilesPerRow`), and each tile is a name and its share on one line over
   * the figure and a thin bar.
   */
  readonly dense?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const style: StyleWithVars = {'--tile-count': String(Math.max(1, props.count))};
  return (
    <section
      className="vs-tiles"
      aria-label={props.label}
      data-stacked={props.stacked === true || undefined}
      data-track={props.track}
      data-dense={props.dense === true || undefined}
      style={style}
    >
      <Dense.Provider value={props.dense === true}>{props.children}</Dense.Provider>
    </section>
  );
}

/**
 * One metric: its `name` in words, the headline `figure` large with a short `note` after it (what
 * the figure means: "faster than input"), a quiet `detail` line below (the current value and the
 * reference value, in the same font and size), and a small `trend` at the bottom. The figure is in
 * the `ok` tone when `tone` says it is an improvement, `err` when a regression, else `muted`. In
 * dense tiles the detail sits on the name's line, at its end, and the figure is a step smaller.
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
  const dense = useContext(Dense);
  return (
    <article className="vs-tile">
      <span className="vs-tile-name">
        <Text variant="small" tone="muted" title={props.name}>
          {props.name}
        </Text>
      </span>
      <div className="vs-tile-figure">
        <Text variant={dense ? 'metric' : 'hero'} tone={props.tone ?? 'default'}>
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
