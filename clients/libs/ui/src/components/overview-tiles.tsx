import type {JSX, ReactNode} from 'react';
import {Menu, type MenuItem} from './menu.js';
import {Chip, StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

/** How an item came out on a chart: kept, not better, failed, or still pending. */
export type SparkMark = 'kept' | 'not-better' | 'failed' | 'pending';

/** One item on a chart: where it sits (`y`, relative to the base) and how it came out. */
export interface SparkPoint {
  readonly y: number;
  readonly mark: SparkMark;
}

const WIDTH = 200;
const HEIGHT = 64;
const PAD = 8;
const MARK = 3;

/**
 * Results in a small chart with no axes: one mark per item in the order the results landed, a
 * dashed `reference` line (the base) and the best-so-far `line`, a step line with one value per
 * point. Filled marks are kept items, hollow ones not better, a cross a failed one and a ringed
 * dot one still pending. The y range fits every point, the line and the reference.
 * `label` describes the chart for assistive technology.
 */
export function ResultChart(props: {
  readonly label: string;
  readonly points: readonly SparkPoint[];
  readonly line: readonly number[];
  readonly reference: number;
}): JSX.Element {
  const {points, line, reference} = props;
  const values = [reference, ...line, ...points.map(point => point.y)];
  const low = Math.min(...values);
  const span = Math.max(...values) - low;
  const y = (value: number): number =>
    span === 0 ? HEIGHT / 2 : HEIGHT - PAD - ((value - low) / span) * (HEIGHT - 2 * PAD);
  const x = (index: number): number =>
    points.length <= 1 ? WIDTH / 2 : PAD + (index * (WIDTH - 2 * PAD)) / (points.length - 1);
  const [first, ...rest] = line;
  const path =
    first === undefined
      ? null
      : `M${x(0)} ${y(first)}${rest.map((value, index) => ` H${x(index + 1)} V${y(value)}`).join('')}`;
  // The marks are positional and never reorder, so a mark's place is its key.
  const marks = points.map((point, index) => ({
    key: `mark-${index}`,
    point,
    cx: x(index),
    cy: y(point.y),
  }));
  return (
    <svg
      className="vs-result-chart"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={props.label}
    >
      <line
        className="vs-result-chart-base"
        x1={0}
        x2={WIDTH}
        y1={y(reference)}
        y2={y(reference)}
      />
      {path === null ? null : <path className="vs-result-chart-line" d={path} />}
      {marks.map(({key, point, cx, cy}) =>
        point.mark === 'failed' ? (
          <path
            key={key}
            className="vs-result-chart-mark"
            data-mark="failed"
            d={`M${cx - MARK} ${cy - MARK}L${cx + MARK} ${cy + MARK}M${cx + MARK} ${cy - MARK}L${cx - MARK} ${cy + MARK}`}
          />
        ) : (
          <circle
            key={key}
            className="vs-result-chart-mark"
            data-mark={point.mark}
            cx={cx}
            cy={cy}
            r={MARK}
          />
        ),
      )}
    </svg>
  );
}

/**
 * One subject at a glance as a tile: its `title` and best `result` against the base, the `facts`
 * line, a `chart`, the `figures` line and what it is doing now with its `status` pill. Pressing
 * it opens the subject; `menu` adds a row menu in its corner. `attention` marks a tile that
 * needs its user.
 */
export function OverviewTile(props: {
  readonly title: string;
  readonly result: {readonly text: string; readonly tone: 'ok' | 'muted'} | null;
  readonly facts: string;
  readonly chart: ReactNode;
  readonly figures: string;
  readonly now: string | null;
  readonly status: {readonly tone: StatusTone; readonly label: string};
  readonly attention?: boolean;
  readonly hint?: string;
  readonly menu?: readonly MenuItem[];
  readonly press: () => void;
}): JSX.Element {
  return (
    <article
      className="vs-overview-tile"
      data-attention={props.attention === true || undefined}
      data-menu={props.menu !== undefined || undefined}
    >
      <button
        type="button"
        className="vs-overview-tile-press"
        title={props.hint}
        onClick={props.press}
      >
        <span className="vs-overview-tile-head">
          <Text variant="item">{props.title}</Text>
          {props.result === null ? null : (
            <Text variant="metric" tone={props.result.tone === 'ok' ? 'ok' : 'muted'}>
              {props.result.text}
            </Text>
          )}
        </span>
        <span className="vs-overview-tile-line">
          <Text variant="small" tone="muted">
            {props.facts}
          </Text>
        </span>
        <span className="vs-overview-tile-chart">{props.chart}</span>
        <span className="vs-overview-tile-line">
          <Text variant="small" tone="muted">
            {props.figures}
          </Text>
        </span>
        <span className="vs-overview-tile-foot">
          <span className="vs-overview-tile-line">
            <Text variant="small">{props.now ?? ' '}</Text>
          </span>
          <Chip label={props.status.label} tone={props.status.tone} dot />
        </span>
      </button>
      {props.menu === undefined ? null : (
        <span className="vs-overview-tile-menu">
          <Menu
            icon="more"
            label={`More for ${props.title}`}
            variant="quiet"
            size="sm"
            items={props.menu}
          />
        </span>
      )}
    </article>
  );
}

/**
 * A titled group of tiles on one grid that wraps to the width: a small status dot (when
 * `tone` is given), the group's `title` and the count of its tiles.
 */
export function OverviewGroup(props: {
  readonly title: string;
  readonly count: number;
  readonly tone?: StatusTone;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <section className="vs-overview-group" aria-label={props.title}>
      <div className="vs-overview-group-head">
        {props.tone === undefined ? null : <StatusDot tone={props.tone} label={props.title} />}
        <Text variant="section">{props.title}</Text>
        <Text variant="section">{String(props.count)}</Text>
      </div>
      <div className="vs-overview-tiles">{props.children}</div>
    </section>
  );
}
