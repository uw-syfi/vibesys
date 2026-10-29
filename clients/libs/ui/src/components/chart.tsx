import type {JSX} from 'react';
import {MONO_ADVANCE, TYPE} from '../tokens/index.js';
import {useInlineSize} from './label-fit.js';

/**
 * A measured point; `filled` points are drawn solid, the rest hollow. A point with an `id` can be
 * `selected` and picked; `label` names it for assistive technology (its `id` if absent).
 */
export interface ChartPoint {
  /** Stable sample identity when several samples open the same item. */
  readonly key?: string;
  readonly id?: string;
  readonly label?: string;
  readonly x: number;
  readonly y: number;
  readonly filled: boolean;
  /** A minor point (a failed try): small and faint. Without it, hollow points turn faint when there are many. */
  readonly faint?: boolean;
  /** The color: the accent (the default), muted grey, or the error red; not text, a mark's own color. */
  readonly tone?: 'accent' | 'muted' | 'err';
  /**
   * How the point stands against the others: `focus` is drawn larger and ringed at full opacity,
   * `related` has a thin outline ring, `dim` is faded. Without it the point is drawn as usual.
   */
  readonly emphasis?: 'focus' | 'related' | 'dim';
  /**
   * On the chain the reader points at: ringed in the accent, over any emphasis. The chart draws no
   * line between points; a chain is read from its ringed dots.
   */
  readonly lit?: boolean;
}

/** A stretch of the x-axis to mark, in the x values' units: a faint band over the plot. */
export interface ChartSpan {
  readonly from: number;
  readonly to: number;
  readonly label?: string;
}

/** A value on an axis and the text it is labelled with. */
export interface ChartTick {
  readonly value: number;
  readonly label: string;
}

/**
 * The drawing is in pixels: its width is its container's, measured, so its text keeps its size
 * whatever the width (`FALLBACK_WIDTH` until measured, never under `MIN_WIDTH`), and its height is
 * fixed: a `strip` shares a row with a figure.
 */
const FALLBACK_WIDTH = 1000;
const MIN_WIDTH = 240;
const HEIGHT = {default: 260, strip: 150} as const;
export type ChartSize = keyof typeof HEIGHT;
/** The x labels sit in the band below the plot. */
const X_BAND = 34;
const PLOT_TOP = 12;
const MARGIN_RIGHT = 10;
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
function plotFor(yTicks: readonly ChartTick[], size: ChartSize, width: number): Plot {
  const longest = Math.max(0, ...yTicks.map(tick => tick.label.length));
  const glyph = TYPE.section.size * MONO_ADVANCE;
  return {
    right: width - MARGIN_RIGHT,
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

function scale(
  plot: Plot,
  xs: readonly number[],
  ys: readonly number[],
  yDomain: readonly [number, number] | undefined,
): Scale {
  const [x0, x1] = extent(xs);
  const [y0, y1] = yDomain ?? extent(ys);
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

/** How far a label's advance box may pass the frame before ink does: glyphs have side bearings. */
const BEARING = 1;

/**
 * Where an x label is drawn for a tick at `x`: centred on it, but kept inside the drawing, so the
 * label of a tick at the plot's end (`333h 20m`) is not cut by the frame; it ends at the frame's
 * edge instead.
 */
function xLabelAt(
  x: number,
  text: string,
  width: number,
): {readonly x: number; readonly anchor: 'start' | 'middle' | 'end'} {
  const half = (text.length * TYPE.section.size * MONO_ADVANCE) / 2;
  if (x + half > width + BEARING) return {x: width, anchor: 'end'};
  if (x - half < -BEARING) return {x: 0, anchor: 'start'};
  return {x, anchor: 'middle'};
}

/** The grid and labels at the ticks, and the two axes. */
function Axes(props: {
  readonly plot: Plot;
  readonly at: Scale;
  readonly width: number;
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
      {xTicks.map(tick => {
        const label = xLabelAt(at.x(tick.value), tick.label, props.width);
        return (
          <text
            key={`x${tick.value}`}
            className="vs-chart-label"
            x={label.x}
            y={plot.bottom + 16}
            textAnchor={label.anchor}
          >
            {tick.label}
          </text>
        );
      })}
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
 * `reference` line, a `playhead` (or a cursor), grid lines and labels at the ticks, and axis titles. Drawn in
 * `accent`, `muted` and `grid` only. `label` describes the chart for assistive technology. `size`
 * `strip` is a short, wide chart. Points with an `id` are buttons that call `pick(id)`; the one
 * whose id is `selected` is drawn larger and ringed. The pickable points are one tab stop (the
 * selected one, else the first); the arrows, Home and End move among them. With many points the
 * hollow ones are small and faint and the filled ones lead; with thousands, the minor ones
 * without an id are binned into at most a few thousand dots.
 */
export function Chart(props: {
  readonly label: string;
  readonly points: readonly ChartPoint[];
  readonly steps?: readonly {readonly x: number; readonly y: number}[];
  readonly reference?: {readonly y: number; readonly label: string};
  /** More dashed lines, each labelled at the right end (the `reference` is labelled at the left). */
  readonly references?: readonly {readonly y: number; readonly label: string}[];
  readonly playhead?: number;
  readonly xTicks?: readonly ChartTick[];
  readonly yTicks?: readonly ChartTick[];
  readonly xTitle?: string;
  /** The y-axis range, fixed by the caller; without it the range fits the points and ticks. */
  readonly yDomain?: readonly [number, number];
  readonly size?: ChartSize;
  readonly selected?: string;
  /** Stretches of the x-axis marked by a faint band; they never change the axis' range. */
  readonly spans?: readonly ChartSpan[];
  readonly pick?: (id: string) => void;
  /** Called with a point's id when the pointer or the focus enters it, and null when it leaves. */
  readonly hover?: (id: string | null) => void;
}): JSX.Element {
  const size = props.size ?? 'default';
  const steps = props.steps ?? [];
  const xTicks = props.xTicks ?? [];
  const yTicks = props.yTicks ?? [];
  const [measure, measured] = useInlineSize();
  const width = Math.max(MIN_WIDTH, measured ?? FALLBACK_WIDTH);
  const plot = plotFor(yTicks, size, width);
  const at = scale(
    plot,
    [...props.points.map(p => p.x), ...steps.map(p => p.x), ...xTicks.map(t => t.value)],
    [
      ...props.points.map(p => p.y),
      ...steps.map(p => p.y),
      ...yTicks.map(t => t.value),
      ...(props.reference === undefined ? [] : [props.reference.y]),
      ...(props.references ?? []).map(line => line.y),
    ],
    props.yDomain,
  );
  const pickable = props.points.filter(point => point.id !== undefined);
  const stop = pickable.some(point => point.id === props.selected)
    ? props.selected
    : pickable[0]?.id;
  return (
    <div className="vs-chart-box" ref={measure}>
      <svg
        className="vs-chart"
        width={width}
        height={HEIGHT[size]}
        viewBox={`0 0 ${width} ${HEIGHT[size]}`}
        role={props.pick === undefined ? 'img' : 'group'}
        aria-label={props.label}
      >
        {(props.spans ?? []).map(span => (
          <Span key={`${span.from}:${span.to}`} span={span} plot={plot} at={at} />
        ))}
        <Axes plot={plot} at={at} width={width} xTicks={xTicks} yTicks={yTicks} />
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
        {(props.references ?? []).map(line => (
          <g key={`${line.label}:${line.y}`} data-reference={line.label}>
            <line
              className="vs-chart-reference"
              x1={plot.left}
              x2={plot.right}
              y1={at.y(line.y)}
              y2={at.y(line.y)}
            />
            <text
              className="vs-chart-label"
              x={plot.right - 6}
              y={at.y(line.y) - 6}
              textAnchor="end"
            >
              {line.label}
            </text>
          </g>
        ))}
        {steps.length === 0 ? null : <path className="vs-chart-step" d={stepPath(steps, at)} />}
        {drawnPoints(props.points).map(point => (
          <Point
            key={point.key ?? point.id ?? `${point.x}:${point.y}`}
            point={point}
            at={at}
            selected={
              point.emphasis === 'focus' || (point.id !== undefined && point.id === props.selected)
            }
            stop={point.id !== undefined && point.id === stop}
            dense={props.points.length > DENSE_ABOVE}
            pick={props.pick}
            hover={props.hover}
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
    </div>
  );
}

/** A span's band over the plot, clipped to it, with a bar on the axis. */
function Span(props: {
  readonly span: ChartSpan;
  readonly plot: Plot;
  readonly at: Scale;
}): JSX.Element {
  const {plot, at, span} = props;
  const left = Math.max(plot.left, Math.min(at.x(span.from), at.x(span.to)));
  const right = Math.min(plot.right, Math.max(at.x(span.from), at.x(span.to)));
  const width = Math.max(0, right - left);
  return (
    <g className="vs-chart-span">
      {span.label === undefined ? null : <title>{span.label}</title>}
      <rect
        className="vs-chart-span-band"
        x={left}
        y={plot.top}
        width={width}
        height={plot.bottom - plot.top}
      />
      <rect
        className="vs-chart-span-bar"
        x={left}
        y={plot.bottom - SPAN_BAR}
        width={width}
        height={SPAN_BAR}
      />
    </g>
  );
}

const SPAN_BAR = 4;
const POINT_RADIUS = 4.5;
const SELECTED_RADIUS = 7;
const LIT_RADIUS = 8.5;
/** With more points than this, the hollow ones are small and faint, so the kept ones lead. */
const DENSE_ABOVE = 60;
const FAINT_RADIUS = 2.5;
/** With more points than this, minor points without an id are binned, one dot per cell of a grid. */
const BIN_ABOVE = 1000;
const BIN_COLUMNS = 100;
const BIN_ROWS = 30;

/**
 * The points to draw: all of them, or, above `BIN_ABOVE`, every filled or identified point and one
 * faint dot for each occupied cell of a grid over the rest, so the node count is bounded.
 */
function drawnPoints(points: readonly ChartPoint[]): readonly ChartPoint[] {
  if (points.length <= BIN_ABOVE) return points;
  const xs = points.map(point => point.x);
  const ys = points.map(point => point.y);
  const [x0, x1] = extent(xs);
  const [y0, y1] = extent(ys);
  const cells = new Map<number, ChartPoint>();
  const kept: ChartPoint[] = [];
  for (const point of points) {
    if (point.filled || point.id !== undefined) {
      kept.push(point);
      continue;
    }
    const column = Math.min(
      BIN_COLUMNS - 1,
      Math.floor(((point.x - x0) / (x1 - x0)) * BIN_COLUMNS),
    );
    const row = Math.min(BIN_ROWS - 1, Math.floor(((point.y - y0) / (y1 - y0)) * BIN_ROWS));
    const cell = row * BIN_COLUMNS + column;
    if (!cells.has(cell)) cells.set(cell, {...point, faint: true});
  }
  return [...cells.values(), ...kept];
}

/** One point; with an `id` and a `pick` it is a button. */
function Point(props: {
  readonly point: ChartPoint;
  readonly at: Scale;
  readonly selected: boolean;
  /** Whether this is the one point of the chart on the tab order. */
  readonly stop: boolean;
  readonly dense: boolean;
  readonly pick: ((id: string) => void) | undefined;
  readonly hover: ((id: string | null) => void) | undefined;
}): JSX.Element {
  const {point, at, selected, pick, hover} = props;
  const faint = point.faint ?? (props.dense && !point.filled);
  const id = point.id;
  const cx = at.x(point.x);
  const cy = at.y(point.y);
  const dot = (
    <circle
      className="vs-chart-point"
      data-filled={point.filled || undefined}
      data-faint={faint || undefined}
      data-emphasis={point.emphasis}
      data-tone={point.tone}
      cx={cx}
      cy={cy}
      r={faint ? FAINT_RADIUS : POINT_RADIUS}
    />
  );
  if (id === undefined || pick === undefined) {
    return (
      <g data-emphasis={point.emphasis}>
        <Rings
          cx={cx}
          cy={cy}
          selected={selected}
          related={point.emphasis === 'related'}
          lit={point.lit === true}
        />
        {dot}
      </g>
    );
  }
  return (
    // biome-ignore lint/a11y/useSemanticElements: an SVG point cannot be a <button>; the group carries the button role, name, state and keys instead.
    <g
      className="vs-chart-pick"
      data-emphasis={point.emphasis}
      role="button"
      tabIndex={props.stop ? 0 : -1}
      aria-label={point.label ?? id}
      aria-pressed={selected}
      onClick={() => pick(id)}
      onMouseEnter={hover === undefined ? undefined : () => hover(id)}
      onMouseLeave={hover === undefined ? undefined : () => hover(null)}
      onFocus={hover === undefined ? undefined : () => hover(id)}
      onBlur={hover === undefined ? undefined : () => hover(null)}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          pick(id);
        } else if (moveAmong(event.currentTarget, event.key)) {
          event.preventDefault();
        }
      }}
    >
      <title>{point.label ?? id}</title>
      <Rings
        cx={cx}
        cy={cy}
        selected={selected}
        related={point.emphasis === 'related'}
        lit={point.lit === true}
      />
      {dot}
    </g>
  );
}

/** Moves the focus among a chart's pickable points by an arrow, Home or End; whether `key` did. */
function moveAmong(current: SVGGElement, key: string): boolean {
  const points = [
    ...(current.ownerSVGElement?.querySelectorAll<SVGGElement>('.vs-chart-pick') ?? []),
  ];
  const index = points.indexOf(current);
  const target: Record<string, number> = {
    ArrowRight: index + 1,
    ArrowUp: index + 1,
    ArrowLeft: index - 1,
    ArrowDown: index - 1,
    Home: 0,
    End: points.length - 1,
  };
  const at = target[key];
  if (at === undefined) return false;
  points[Math.max(0, Math.min(points.length - 1, at))]?.focus();
  return true;
}

/** The ring of the selected point, the thin outline of a related one, or the accent ring of a lit one. */
function Rings(props: {
  readonly cx: number;
  readonly cy: number;
  readonly selected: boolean;
  readonly related: boolean;
  readonly lit: boolean;
}): JSX.Element {
  return (
    <>
      {props.lit ? (
        <circle className="vs-chart-lit" cx={props.cx} cy={props.cy} r={LIT_RADIUS} />
      ) : null}
      {props.selected ? (
        <circle className="vs-chart-ring" cx={props.cx} cy={props.cy} r={SELECTED_RADIUS} />
      ) : null}
      {props.related ? (
        <circle className="vs-chart-related" cx={props.cx} cy={props.cy} r={SELECTED_RADIUS} />
      ) : null}
    </>
  );
}
