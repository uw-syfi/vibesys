import {type JSX, type ReactNode, useId, useState} from 'react';
import {MONO_ADVANCE, TYPE} from '../tokens/index.js';
import {useInlineSize} from './label-fit.js';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/**
 * What hovering a point says, short enough to stay inside the plot: its `detail` (the figures) is
 * always whole, led by `lead` (what the point is), which is cut with an ellipsis to the room left.
 */
export interface ChartTip {
  readonly lead: string;
  readonly detail: string;
}

/**
 * A reading of a series: a dot at (`x`, `y`). A point with an `id` can be picked; `label` names it
 * for assistive technology and is what hovering it says (its `id` if absent) unless it has a `tip`.
 */
export interface ChartPoint {
  /** Stable sample identity when several samples open the same item. */
  readonly key?: string;
  readonly id?: string;
  readonly label?: string;
  readonly tip?: ChartTip;
  readonly x: number;
  readonly y: number;
  /** Singled out: ringed in the text color. */
  readonly selected?: boolean;
  /**
   * On the chain the reader points at: ringed in the accent, over a ring of `selected`. The chart
   * draws no line between points; a chain is read from its ringed dots.
   */
  readonly lit?: boolean;
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
const HEIGHT = {default: 260, strip: 150, compact: 180} as const;
export type ChartSize = keyof typeof HEIGHT;
/** The x labels sit in the band below the plot; a caption adds a line under them. */
const X_BAND = 20;
const CAPTION_BAND = 20;
const PLOT_TOP = 12;
/** The room above the plot for the unit the y axis reads in, written at the drawing's left edge. */
const UNIT_BAND = 24;
const MARGIN_RIGHT = 10;
/** The gap between a y label and the axis, and the margin left of the longest label. */
const LABEL_GAP = 8;
const EDGE = 4;
/** How far a mark on the x axis rises from it. */
const MARK_HEIGHT = 8;

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
function plotFor(
  yTicks: readonly ChartTick[],
  size: ChartSize,
  width: number,
  captioned: boolean,
  united: boolean,
): Plot {
  const longest = Math.max(0, ...yTicks.map(tick => tick.label.length));
  const glyph = TYPE.section.size * MONO_ADVANCE;
  return {
    right: width - MARGIN_RIGHT,
    top: united ? UNIT_BAND : PLOT_TOP,
    bottom: HEIGHT[size] - X_BAND - (captioned ? CAPTION_BAND : 0),
    left: EDGE + Math.ceil(longest * glyph) + LABEL_GAP,
  };
}

interface Scale {
  readonly x: (value: number) => number;
  readonly y: (value: number) => number;
}

function extent(values: readonly number[]): readonly [number, number] {
  // Not `Math.min(...values)`: a series of a hundred thousand readings has more than a call takes.
  let [low, high] = [Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY];
  for (const value of values) {
    if (value < low) low = value;
    if (value > high) high = value;
  }
  return low === high ? [low - 1, high + 1] : [low, high];
}

/** `range`, or one unit either side of it when it is a single value, so nothing divides by zero. */
function widened(range: readonly [number, number]): readonly [number, number] {
  return range[0] === range[1] ? [range[0] - 1, range[1] + 1] : range;
}

function scale(
  plot: Plot,
  xs: readonly number[],
  ys: readonly number[],
  xDomain: readonly [number, number] | undefined,
  yDomain: readonly [number, number] | undefined,
): Scale {
  const [x0, x1] = widened(xDomain ?? extent(xs));
  const [y0, y1] = widened(yDomain ?? extent(ys));
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

/** A small mark on the x axis, rising from it. */
function Mark(props: {readonly x: number; readonly bottom: number}): JSX.Element {
  return (
    <line
      className="vs-chart-mark"
      x1={props.x}
      x2={props.x}
      y1={props.bottom - MARK_HEIGHT}
      y2={props.bottom}
    />
  );
}

/**
 * The line under the x labels: the `caption` at the axis' start, and, when the chart has `marks`, a
 * sample of one with `markLabel` at its end.
 */
function Caption(props: {
  readonly plot: Plot;
  readonly caption: string;
  readonly markLabel: string | undefined;
}): JSX.Element {
  const {plot} = props;
  const y = plot.bottom + 16 + CAPTION_BAND;
  const glyph = TYPE.section.size * MONO_ADVANCE;
  const sample =
    props.markLabel === undefined ? null : plot.right - props.markLabel.length * glyph - LABEL_GAP;
  return (
    <>
      <text className="vs-chart-label" x={plot.left} y={y}>
        {props.caption}
      </text>
      {props.markLabel === undefined || sample === null ? null : (
        <>
          <Mark x={sample} bottom={y + 1} />
          <text className="vs-chart-label" x={plot.right} y={y} textAnchor="end">
            {props.markLabel}
          </text>
        </>
      )}
    </>
  );
}

/** Dashed reference lines, each labelled at its right end. */
function References(props: {
  readonly lines: readonly {readonly y: number; readonly label: string}[];
  readonly plot: Plot;
  readonly at: Scale;
}): JSX.Element {
  const {plot, at} = props;
  return (
    <>
      {props.lines.map(line => (
        <g key={`${line.label}:${line.y}`} data-reference={line.label}>
          <line
            className="vs-chart-reference"
            x1={plot.left}
            x2={plot.right}
            y1={at.y(line.y)}
            y2={at.y(line.y)}
          />
          <text className="vs-chart-label" x={plot.right - 6} y={at.y(line.y) - 6} textAnchor="end">
            {line.label}
          </text>
        </g>
      ))}
    </>
  );
}

/** What stands on and under the x axis besides its labels: the marks on it, and the caption. */
function Footer(props: {
  readonly plot: Plot;
  readonly at: Scale;
  readonly marks: readonly number[];
  readonly caption: string | undefined;
  readonly markLabel: string | undefined;
}): JSX.Element {
  const {plot, at, marks} = props;
  return (
    <>
      {marks.map(mark => (
        <Mark key={mark} x={at.x(mark)} bottom={plot.bottom} />
      ))}
      {props.caption === undefined ? null : (
        <Caption
          plot={plot}
          caption={props.caption}
          markLabel={marks.length === 0 ? undefined : props.markLabel}
        />
      )}
    </>
  );
}

interface ChartProps {
  readonly label: string;
  readonly points: readonly ChartPoint[];
  readonly steps?: readonly {readonly x: number; readonly y: number}[];
  /** What the line's first step is of (`Input`), written above it. */
  readonly start?: string;
  /** Dashed lines, each labelled at the right end. */
  readonly references?: readonly {readonly y: number; readonly label: string}[];
  /** Where the reader stands on the x axis: a line there, and the dots and line after it dimmed. */
  readonly cursor?: number;
  /** Small marks on the x axis, at these x values. */
  readonly marks?: readonly number[];
  /** What a mark is, in a word, with a sample of one beside the caption (only with `marks`). */
  readonly markLabel?: string;
  /** What the x axis measures, under its labels. */
  readonly caption?: string;
  /** The unit the y axis reads in, written above its top. */
  readonly unit?: string;
  readonly xTicks?: readonly ChartTick[];
  readonly yTicks?: readonly ChartTick[];
  /** The x-axis range, fixed by the caller; without it the range fits the points, steps and ticks. */
  readonly xDomain?: readonly [number, number];
  /** The y-axis range, fixed by the caller; without it the range fits the points and ticks. */
  readonly yDomain?: readonly [number, number];
  readonly size?: ChartSize;
  readonly pick?: (id: string) => void;
  /** Called with a point's id when the pointer or the focus enters it, and null when it leaves. */
  readonly hover?: (id: string | null) => void;
}

/** The plot, the scales that place a value on it, and the one pickable point on the tab order. */
function layoutOf(
  props: ChartProps,
  width: number,
): {readonly plot: Plot; readonly at: Scale; readonly stop: string | undefined} {
  const steps = props.steps ?? [];
  const plot = plotFor(
    props.yTicks ?? [],
    props.size ?? 'default',
    width,
    props.caption !== undefined,
    props.unit !== undefined,
  );
  const at = scale(
    plot,
    [
      ...props.points.map(p => p.x),
      ...steps.map(p => p.x),
      ...(props.xTicks ?? []).map(t => t.value),
    ],
    [
      ...props.points.map(p => p.y),
      ...steps.map(p => p.y),
      ...(props.yTicks ?? []).map(t => t.value),
      ...(props.references ?? []).map(line => line.y),
    ],
    props.xDomain,
    props.yDomain,
  );
  const pickable = props.points.filter(point => point.id !== undefined);
  return {plot, at, stop: (pickable.find(point => point.selected === true) ?? pickable[0])?.id};
}

/**
 * A series over time or index in SVG: `points` (one faint dot each), a `steps` line (the best so
 * far, starting at `start`'s value and stepping at each new best), dashed `references`, a `cursor`
 * (the line and the dots after it dimmed), marks on the x axis, grid lines and labels at the ticks,
 * and a `caption` under them. Drawn in `accent`, `muted`, the text color and `grid` only. `label`
 * describes the chart for assistive technology. `size` `strip` is a short, wide chart. Points with an
 * `id` are buttons that call `pick(id)` (hovering or focusing one says its `label` in a tip), and the
 * pickable points are one tab stop (the selected one, else the first); the arrows, Home and End
 * move among them. With many points the dots are small, and with thousands, those without an id
 * are binned into at most a few thousand dots.
 */
export function Chart(props: ChartProps): JSX.Element {
  const size = props.size ?? 'default';
  const steps = props.steps ?? [];
  const xTicks = props.xTicks ?? [];
  const yTicks = props.yTicks ?? [];
  const marks = props.marks ?? [];
  const [measure, measured] = useInlineSize();
  const [tip, setTip] = useState<Told>();
  const clip = useId().replaceAll(':', '');
  const width = Math.max(MIN_WIDTH, measured ?? FALLBACK_WIDTH);
  const {plot, at, stop} = layoutOf(props, width);
  const cursor = props.cursor === undefined ? undefined : at.x(props.cursor);
  const height = HEIGHT[size];
  const first = steps[0];
  return (
    <div className="vs-chart-box" ref={measure}>
      <svg
        className="vs-chart"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role={props.pick === undefined ? 'img' : 'group'}
        aria-label={props.label}
      >
        {props.unit === undefined ? null : (
          <text className="vs-chart-label" x={0} y={UNIT_BAND - 9}>
            {props.unit}
          </text>
        )}
        <Axes plot={plot} at={at} width={width} xTicks={xTicks} yTicks={yTicks} />
        <Footer
          plot={plot}
          at={at}
          marks={marks}
          caption={props.caption}
          markLabel={props.markLabel}
        />
        <References lines={props.references ?? []} plot={plot} at={at} />
        {first === undefined || props.start === undefined ? null : (
          <text className="vs-chart-label" x={plot.left + 6} y={at.y(first.y) - 8}>
            {props.start}
          </text>
        )}
        {steps.length === 0 ? null : (
          <Line d={stepPath(steps, at)} cursor={cursor} plot={plot} height={height} id={clip} />
        )}
        {drawnPoints(props.points).map(point => (
          <Point
            key={point.key ?? point.id ?? `${point.x}:${point.y}`}
            point={point}
            at={at}
            after={props.cursor !== undefined && point.x > props.cursor}
            stop={point.id !== undefined && point.id === stop}
            dense={props.points.length > DENSE_ABOVE}
            pick={props.pick}
            hover={props.hover}
            tell={setTip}
          />
        ))}
        {cursor === undefined ? null : (
          <line
            className="vs-chart-cursor"
            x1={cursor}
            x2={cursor}
            y1={plot.top}
            y2={plot.bottom}
          />
        )}
        {tip === undefined ? null : <Tip tip={tip} plot={plot} />}
      </svg>
    </div>
  );
}

/**
 * The best-so-far line. With a cursor, the part after it is drawn again, dimmed, over the clipped
 * halves of one path, so the line is one shape the cursor only divides.
 */
function Line(props: {
  readonly d: string;
  readonly cursor: number | undefined;
  readonly plot: Plot;
  readonly height: number;
  readonly id: string;
}): JSX.Element {
  const {cursor, plot, id} = props;
  if (cursor === undefined) return <path className="vs-chart-step" d={props.d} />;
  const edge = Math.min(plot.right, Math.max(plot.left, cursor));
  return (
    <>
      <clipPath id={`${id}-before`}>
        <rect x={0} y={0} width={edge} height={props.height} />
      </clipPath>
      <clipPath id={`${id}-after`}>
        <rect
          x={edge}
          y={0}
          width={Math.max(0, plot.right + MARGIN_RIGHT - edge)}
          height={props.height}
        />
      </clipPath>
      <path className="vs-chart-step" d={props.d} clipPath={`url(#${id}-before)`} />
      <path className="vs-chart-step" data-after="" d={props.d} clipPath={`url(#${id}-after)`} />
    </>
  );
}

const POINT_RADIUS = 3.5;
const SELECTED_RADIUS = 7;
const LIT_RADIUS = 8.5;
/** With more points than this, the dots are small, so the ringed ones lead. */
const DENSE_ABOVE = 60;
const FAINT_RADIUS = 2.5;
/** With more points than this, minor points without an id are binned, one dot per cell of a grid. */
const BIN_ABOVE = 1000;
const BIN_COLUMNS = 100;
const BIN_ROWS = 30;

/**
 * The points to draw: all of them, or, above `BIN_ABOVE`, every identified or ringed point and one
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
    if (point.selected === true || point.lit === true || point.id !== undefined) {
      kept.push(point);
      continue;
    }
    const column = Math.min(
      BIN_COLUMNS - 1,
      Math.floor(((point.x - x0) / (x1 - x0)) * BIN_COLUMNS),
    );
    const row = Math.min(BIN_ROWS - 1, Math.floor(((point.y - y0) / (y1 - y0)) * BIN_ROWS));
    const cell = row * BIN_COLUMNS + column;
    if (!cells.has(cell)) cells.set(cell, point);
  }
  return [...cells.values(), ...kept];
}

/** One point; with an `id` and a `pick` it is a button. */
function Point(props: {
  readonly point: ChartPoint;
  readonly at: Scale;
  /** After the cursor: drawn dimmed. */
  readonly after: boolean;
  /** Whether this is the one point of the chart on the tab order. */
  readonly stop: boolean;
  readonly dense: boolean;
  readonly pick: ((id: string) => void) | undefined;
  readonly hover: ((id: string | null) => void) | undefined;
  readonly tell: (tip: Told | undefined) => void;
}): JSX.Element {
  const {point, at, pick, hover} = props;
  const id = point.id;
  const cx = at.x(point.x);
  const cy = at.y(point.y);
  const text = point.label ?? id;
  const said = point.tip ?? (text === undefined ? undefined : {lead: text, detail: ''});
  const enter = (): void => {
    if (said !== undefined) props.tell({...said, x: cx, y: cy});
    if (id !== undefined) hover?.(id);
  };
  const leave = (): void => {
    props.tell(undefined);
    if (id !== undefined) hover?.(null);
  };
  const body = (
    <>
      <Rings cx={cx} cy={cy} selected={point.selected === true} lit={point.lit === true} />
      <circle
        className="vs-chart-point"
        data-after={props.after || undefined}
        cx={cx}
        cy={cy}
        r={props.dense ? FAINT_RADIUS : POINT_RADIUS}
      />
    </>
  );
  if (id === undefined || pick === undefined) return <g>{body}</g>;
  return (
    // biome-ignore lint/a11y/useSemanticElements: an SVG point cannot be a <button>; the group carries the button role, name, state and keys instead.
    <g
      className="vs-chart-pick"
      role="button"
      tabIndex={props.stop ? 0 : -1}
      aria-label={text}
      aria-pressed={point.selected === true}
      onClick={() => pick(id)}
      onMouseEnter={enter}
      onMouseLeave={leave}
      onFocus={enter}
      onBlur={leave}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          pick(id);
        } else if (moveAmong(event.currentTarget, event.key)) {
          event.preventDefault();
        }
      }}
    >
      {body}
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

/** The ring of the selected point (the text color), and the accent ring of one on the chain. */
function Rings(props: {
  readonly cx: number;
  readonly cy: number;
  readonly selected: boolean;
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
    </>
  );
}

const TIP_PAD = 8;
const TIP_HEIGHT = 26;
const TIP_GAP = 12;
const TIP_SEPARATOR = ' · ';

/** What a hovered point says and where: the words (`ChartTip`) and the point they belong to. */
interface Told extends ChartTip {
  readonly x: number;
  readonly y: number;
}

/** `text` in at most `chars` characters, the end cut with an ellipsis when it does not fit. */
function cut(text: string, chars: number): string {
  return text.length <= chars ? text : `${text.slice(0, Math.max(0, chars - 1)).trimEnd()}…`;
}

/**
 * The words of a tip in at most `chars` characters: the detail whole, and the lead before it as far
 * as it fits (cut with an ellipsis; left out when there is no room for more than a letter).
 */
function tipWords(tip: ChartTip, chars: number): string {
  if (tip.detail === '') return cut(tip.lead, chars);
  const room = chars - tip.detail.length - TIP_SEPARATOR.length;
  return room < 2 || tip.lead === ''
    ? tip.detail
    : `${cut(tip.lead, room)}${TIP_SEPARATOR}${tip.detail}`;
}

/** What a hovered point says, in a small box by it kept inside the plot. */
function Tip(props: {readonly tip: Told; readonly plot: Plot}): JSX.Element {
  const {tip, plot} = props;
  const glyph = TYPE.section.size * MONO_ADVANCE;
  const room = Math.floor((plot.right - plot.left - 2 * TIP_PAD) / glyph);
  const words = tipWords(tip, room);
  const width = Math.ceil(words.length * glyph) + 2 * TIP_PAD;
  const left = Math.max(plot.left, Math.min(plot.right - width, tip.x - width / 2));
  const above = tip.y - TIP_GAP - TIP_HEIGHT >= plot.top;
  const top = above ? tip.y - TIP_GAP - TIP_HEIGHT : tip.y + TIP_GAP;
  return (
    <g className="vs-chart-tip" pointerEvents="none">
      <rect
        className="vs-chart-tip-box"
        x={left}
        y={top}
        width={width}
        height={TIP_HEIGHT}
        rx={4}
      />
      <text
        className="vs-chart-tip-text"
        x={left + TIP_PAD}
        y={top + TIP_HEIGHT / 2}
        dominantBaseline="middle"
      >
        {words}
      </text>
    </g>
  );
}

/** A mark of a key: the line, a dot, or a ring. */
export type ChartLegendMark = 'line' | 'dot' | 'ring';

/** One entry of a `ChartLegend`. */
export interface ChartLegendItem {
  readonly label: string;
  readonly mark: ChartLegendMark;
}

/** A one-line key to what a chart draws: each mark as the chart draws it, and its word. */
export function ChartLegend(props: {
  readonly label: string;
  readonly items: readonly ChartLegendItem[];
}): JSX.Element {
  return (
    <ul className="vs-dotlegend vs-serieslegend vs-chart-legend" aria-label={props.label}>
      {props.items.map(item => (
        <li key={item.label} className="vs-dotlegend-item">
          <span className="vs-chart-legend-mark" data-mark={item.mark} aria-hidden="true" />
          <Text variant="small" tone="muted">
            {item.label}
          </Text>
        </li>
      ))}
    </ul>
  );
}

/**
 * A chart under its heading: the `title` and what it `note`s (which way is better), and at the far
 * end a `figure` in the tone of its meaning. A figure with a `hint` takes the keyboard focus and
 * exposes the hint as its accessible description; the hint is drawn under it on hover and on focus
 * (as a chip's is), so the exact numbers behind the figure need no pointer. The chart is `children`.
 */
export function ChartPanel(props: {
  readonly title: string;
  readonly note?: string;
  /**
   * The heading stays on one line, so charts side by side start at the same height: the note is cut
   * with an ellipsis (its words on hover) before the figure goes under the name.
   */
  readonly oneLine?: boolean;
  readonly figure?: string;
  readonly tone?: 'ok' | 'err' | 'muted';
  readonly hint?: string;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <section className="vs-chart-panel" aria-label={props.title}>
      <div className="vs-chart-panel-head" data-one-line={props.oneLine || undefined}>
        <span className="vs-chart-panel-title">
          <Text variant="item">{props.title}</Text>
          {props.note === undefined ? null : (
            <span className="vs-chart-panel-note" title={props.note}>
              <Text variant="small" tone="muted">
                {props.note}
              </Text>
            </span>
          )}
        </span>
        {props.figure === undefined ? null : (
          <span
            className="vs-chart-figure"
            aria-description={props.hint}
            // As a chip's hint: the numbers behind the figure are information a keyboard and a screen
            // reader must reach, which a `title` is not, so a figure with a hint is a tab stop.
            tabIndex={props.hint === undefined ? undefined : 0}
          >
            <Text
              variant="body-strong"
              tone={props.tone === 'ok' || props.tone === 'err' ? props.tone : 'muted'}
            >
              {props.figure}
            </Text>
          </span>
        )}
      </div>
      {props.children}
    </section>
  );
}

/**
 * Charts side by side, as wide as the others and none narrower than a chart and its axis titles can be read:
 * as many to a row as fit, the rest on the rows below.
 */
export function ChartColumns(props: {
  readonly children: ReactNode;
  /** At most this many to a row, equally wide whatever the room; the rest wrap below. */
  readonly across?: number;
}): JSX.Element {
  const style: StyleWithVars | undefined =
    props.across === undefined ? undefined : {'--across': props.across};
  return (
    <div className="vs-chart-columns" data-across={props.across} style={style}>
      {props.children}
    </div>
  );
}
