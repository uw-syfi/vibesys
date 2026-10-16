import type {JSX} from 'react';
import {MONO_ADVANCE, TYPE} from '../tokens/index.js';

/**
 * A measured point; `filled` points are drawn solid, the rest hollow. A point with an `id` can be
 * `selected` and picked; `label` names it for assistive technology (its `id` if absent).
 */
export interface ChartPoint {
  readonly id?: string;
  readonly label?: string;
  readonly x: number;
  readonly y: number;
  readonly filled: boolean;
}

/** A value on an axis and the text it is labelled with. */
export interface ChartTick {
  readonly value: number;
  readonly label: string;
}

/** The drawing's own coordinates: the SVG scales to its container's width. */
const WIDTH = 1000;
/** A chart's height in those coordinates: `default`, or a `strip` that shares a row with a figure. */
const HEIGHT = {default: 300, strip: 170} as const;
export type ChartSize = keyof typeof HEIGHT;
/** The x labels sit in the band below the plot. */
const X_BAND = 34;
const PLOT_TOP = 12;
const PLOT_RIGHT = 990;
/** The gap between a y label and the axis, and the margin left of the longest label. */
const LABEL_GAP = 8;
const EDGE = 4;

interface Plot {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
}

/**
 * The plot area, its left gutter as wide as the longest y label needs, so a label is never cut at
 * the drawing's edge whatever its length. The labels are drawn in the mono `section` style, each
 * glyph `MONO_ADVANCE` em wide.
 */
function plotFor(yTicks: readonly ChartTick[], size: ChartSize): Plot {
  const longest = Math.max(0, ...yTicks.map(tick => tick.label.length));
  const glyph = TYPE.section.size * MONO_ADVANCE;
  return {
    right: PLOT_RIGHT,
    top: PLOT_TOP,
    bottom: HEIGHT[size] - X_BAND,
    left: EDGE + Math.ceil(longest * glyph) + LABEL_GAP,
  };
}

interface Scale {
  readonly x: (value: number) => number;
  readonly y: (value: number) => number;
}

function extent(values: readonly number[]): readonly [number, number] {
  const low = Math.min(...values);
  const high = Math.max(...values);
  return low === high ? [low - 1, high + 1] : [low, high];
}

function scale(plot: Plot, xs: readonly number[], ys: readonly number[]): Scale {
  const [x0, x1] = extent(xs);
  const [y0, y1] = extent(ys);
  return {
    x: value => plot.left + ((value - x0) / (x1 - x0)) * (plot.right - plot.left),
    y: value => plot.bottom - ((value - y0) / (y1 - y0)) * (plot.bottom - plot.top),
  };
}

/** The best-so-far path through `steps`: across at each value, then up or down to the next. */
function stepPath(steps: readonly {readonly x: number; readonly y: number}[], at: Scale): string {
  return steps
    .map((point, index) =>
      index === 0 ? `M${at.x(point.x)} ${at.y(point.y)}` : `H${at.x(point.x)} V${at.y(point.y)}`,
    )
    .join(' ');
}

/** The grid and labels at the ticks, and the two axes. */
function Axes(props: {
  readonly plot: Plot;
  readonly at: Scale;
  readonly xTicks: readonly ChartTick[];
  readonly yTicks: readonly ChartTick[];
}): JSX.Element {
  const {plot, at, xTicks, yTicks} = props;
  return (
    <>
      {yTicks.map(tick => (
        <g key={`y${tick.value}`}>
          <line
            className="vs-chart-grid"
            x1={plot.left}
            x2={plot.right}
            y1={at.y(tick.value)}
            y2={at.y(tick.value)}
          />
          <text
            className="vs-chart-label"
            x={plot.left - LABEL_GAP}
            y={at.y(tick.value)}
            textAnchor="end"
            dominantBaseline="middle"
          >
            {tick.label}
          </text>
        </g>
      ))}
      {xTicks.map(tick => (
        <text
          key={`x${tick.value}`}
          className="vs-chart-label"
          x={at.x(tick.value)}
          y={plot.bottom + 16}
          textAnchor="middle"
        >
          {tick.label}
        </text>
      ))}
      <line
        className="vs-chart-axis"
        x1={plot.left}
        x2={plot.left}
        y1={plot.top}
        y2={plot.bottom}
      />
      <line
        className="vs-chart-axis"
        x1={plot.left}
        x2={plot.right}
        y1={plot.bottom}
        y2={plot.bottom}
      />
    </>
  );
}

/**
 * A series over time or index in SVG: `points` (filled or hollow), a `steps` line, a dashed
 * `reference` line, a `playhead`, grid lines and labels at the ticks, and axis titles. Drawn in
 * `accent`, `muted` and `grid` only. `label` describes the chart for assistive technology. `size`
 * `strip` is a short, wide chart. Points with an `id` are buttons that call `pick(id)`; the one
 * whose id is `selected` is drawn larger and ringed.
 */
export function Chart(props: {
  readonly label: string;
  readonly points: readonly ChartPoint[];
  readonly steps?: readonly {readonly x: number; readonly y: number}[];
  readonly reference?: {readonly y: number; readonly label: string};
  readonly playhead?: number;
  readonly xTicks?: readonly ChartTick[];
  readonly yTicks?: readonly ChartTick[];
  readonly xTitle?: string;
  readonly size?: ChartSize;
  readonly selected?: string;
  readonly pick?: (id: string) => void;
}): JSX.Element {
  const size = props.size ?? 'default';
  const steps = props.steps ?? [];
  const xTicks = props.xTicks ?? [];
  const yTicks = props.yTicks ?? [];
  const plot = plotFor(yTicks, size);
  const at = scale(
    plot,
    [...props.points.map(p => p.x), ...steps.map(p => p.x), ...xTicks.map(t => t.value)],
    [
      ...props.points.map(p => p.y),
      ...steps.map(p => p.y),
      ...yTicks.map(t => t.value),
      ...(props.reference === undefined ? [] : [props.reference.y]),
    ],
  );
  return (
    <svg
      className="vs-chart"
      viewBox={`0 0 ${WIDTH} ${HEIGHT[size]}`}
      role={props.pick === undefined ? 'img' : 'group'}
      aria-label={props.label}
    >
      <Axes plot={plot} at={at} xTicks={xTicks} yTicks={yTicks} />
      {props.reference === undefined ? null : (
        <g>
          <line
            className="vs-chart-reference"
            x1={plot.left}
            x2={plot.right}
            y1={at.y(props.reference.y)}
            y2={at.y(props.reference.y)}
          />
          <text className="vs-chart-label" x={plot.left + 6} y={at.y(props.reference.y) - 6}>
            {props.reference.label}
          </text>
        </g>
      )}
      {steps.length === 0 ? null : <path className="vs-chart-step" d={stepPath(steps, at)} />}
      {props.points.map(point => (
        <Point
          key={point.id ?? `${point.x}:${point.y}`}
          point={point}
          at={at}
          selected={point.id !== undefined && point.id === props.selected}
          pick={props.pick}
        />
      ))}
      {props.playhead === undefined ? null : (
        <line
          className="vs-chart-playhead"
          x1={at.x(props.playhead)}
          x2={at.x(props.playhead)}
          y1={plot.top}
          y2={plot.bottom}
        />
      )}
      {props.xTitle === undefined ? null : (
        <text className="vs-chart-label" x={plot.right} y={HEIGHT[size] - 2} textAnchor="end">
          {props.xTitle}
        </text>
      )}
    </svg>
  );
}

const POINT_RADIUS = 5.5;
const SELECTED_RADIUS = 8;

/** One point; with an `id` and a `pick` it is a button. */
function Point(props: {
  readonly point: ChartPoint;
  readonly at: Scale;
  readonly selected: boolean;
  readonly pick: ((id: string) => void) | undefined;
}): JSX.Element {
  const {point, at, selected, pick} = props;
  const id = point.id;
  const cx = at.x(point.x);
  const cy = at.y(point.y);
  const dot = (
    <circle
      className="vs-chart-point"
      data-filled={point.filled || undefined}
      cx={cx}
      cy={cy}
      r={POINT_RADIUS}
    />
  );
  if (id === undefined || pick === undefined) {
    return selected ? (
      <SelectedPoint cx={cx} cy={cy}>
        {dot}
      </SelectedPoint>
    ) : (
      dot
    );
  }
  return (
    // biome-ignore lint/a11y/useSemanticElements: an SVG point cannot be a <button>; the group carries the button role, name, state and keys instead.
    <g
      className="vs-chart-pick"
      role="button"
      tabIndex={0}
      aria-label={point.label ?? id}
      aria-pressed={selected}
      onClick={() => pick(id)}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          pick(id);
        }
      }}
    >
      <title>{point.label ?? id}</title>
      {selected ? <circle className="vs-chart-ring" cx={cx} cy={cy} r={SELECTED_RADIUS} /> : null}
      {dot}
    </g>
  );
}

function SelectedPoint(props: {
  readonly cx: number;
  readonly cy: number;
  readonly children: JSX.Element;
}): JSX.Element {
  return (
    <g>
      <circle className="vs-chart-ring" cx={props.cx} cy={props.cy} r={SELECTED_RADIUS} />
      {props.children}
    </g>
  );
}
