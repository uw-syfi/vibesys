import {type JSX, useState} from 'react';
import {MONO_ADVANCE, TYPE} from '../tokens/index.js';
import {useInlineSize} from './label-fit.js';
import {type Direction, frontierAt, type Tradeoff, toward} from './pareto-frontier.js';
import {Text} from './text.js';

/**
 * How a measured point ended: the `input` is the starting point; a `passed` point competes for
 * the frontier; a `failed` point was measured but failed, so it is drawn and never competes.
 */
export type ParetoState = 'input' | 'passed' | 'failed';

/** One measured point of a two-objective trade-off. `time` is when it was measured, in the cursor's unit. */
export interface ParetoPoint extends Tradeoff {
  readonly id: string;
  /** What hovering names it; its `id` if absent. */
  readonly label?: string;
  readonly state: ParetoState;
}

/** What one axis measures, in what unit, and which way is better. */
export interface ParetoAxis {
  readonly name: string;
  readonly unit: string;
  readonly direction: Direction;
}

type Role = 'input' | 'frontier' | 'dominated' | 'failed';

/**
 * The drawing is in pixels: its width is its container's, measured, so its text keeps its size
 * whatever the width (`FALLBACK_WIDTH` until measured), and `HEIGHT` is fixed.
 */
const FALLBACK_WIDTH = 1000;
const MIN_WIDTH = 288;
const HEIGHT = {default: 380, strip: 260} as const;
export type ParetoSize = keyof typeof HEIGHT;
/** Bands for the axis titles, above and below the plot, and the gutter around it. */
const TOP_BAND = 28;
const BOTTOM_BAND = 54;
const MARGIN_RIGHT = 16;
const EDGE = 4;
const LABEL_GAP = 8;
/** The plot's range is the data's, widened by this share, so no point sits on an axis. */
const PAD = 0.07;
/** About this many ticks along the long, horizontal axis and the short, vertical one. */
const X_TICKS = 6;
const Y_TICKS = 4;
const R = 6;
const R_SMALL = 4;
const R_RING = 10;
const GLYPH = TYPE.section.size * MONO_ADVANCE;
const KEY_ORDER: readonly Role[] = ['input', 'frontier', 'dominated', 'failed'];

interface Plot {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
}

/** An axis laid out on the plot: where a value falls, and the ticks and their words. */
interface Layout {
  /** A position along the axis from 0 (its start) to 1 (its end); larger is always better. */
  readonly share: (value: number) => number;
  readonly ticks: readonly {readonly value: number; readonly label: string}[];
}

/** The plot's area and the scales that place a value on it. */
interface Frame {
  readonly plot: Plot;
  readonly horizontal: Layout;
  readonly vertical: Layout;
  readonly px: (value: number) => number;
  readonly py: (value: number) => number;
}

/** The tick values: a few even numbers inside `[low, high]`. */
function niceTicks(low: number, high: number, wanted: number): readonly number[] {
  const raw = (high - low) / wanted;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].map(m => m * magnitude).find(s => s >= raw) ?? raw) || 1;
  const first = Math.ceil(low / step) * step;
  const count = Math.floor((high - first) / step + 1e-9) + 1;
  return Array.from({length: Math.max(0, count)}, (_, index) =>
    Number((first + index * step).toPrecision(12)),
  );
}

const COMPACT = new Intl.NumberFormat('en-US', {notation: 'compact', maximumFractionDigits: 2});

/**
 * A value as a hover writes it: grouped, never rounded to hundreds, with fewer decimals the larger
 * it is (`6,054,154`, `47.2`, `3.84`, `0.512`).
 */
function exact(value: number): string {
  const magnitude = Math.abs(value);
  const digits = magnitude >= 100 ? 0 : magnitude >= 10 ? 1 : magnitude >= 1 ? 2 : 3;
  return value.toLocaleString('en-US', {maximumFractionDigits: digits});
}

function layoutAxis(measured: readonly number[], direction: Direction, count: number): Layout {
  const values = measured.length === 0 ? [0, 1] : measured;
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high === low ? Math.max(1, Math.abs(low) * 0.1) : high - low;
  const from = (high === low ? low - span : low) - span * PAD;
  const to = (high === low ? high + span : high) + span * PAD;
  const along = (value: number): number => (value - from) / (to - from);
  return {
    share: value => (direction === 'higher' ? along(value) : 1 - along(value)),
    ticks: niceTicks(from, to, count).map(value => ({value, label: COMPACT.format(value)})),
  };
}

function frameFor(
  points: readonly ParetoPoint[],
  x: ParetoAxis,
  y: ParetoAxis,
  width: number,
  height: number,
): Frame {
  const horizontal = layoutAxis(
    points.map(p => p.x),
    x.direction,
    X_TICKS,
  );
  const vertical = layoutAxis(
    points.map(p => p.y),
    y.direction,
    Y_TICKS,
  );
  const widest = Math.max(0, ...vertical.ticks.map(t => t.label.length));
  const plot: Plot = {
    left: EDGE + Math.ceil(widest * GLYPH) + LABEL_GAP,
    right: width - MARGIN_RIGHT,
    top: TOP_BAND,
    bottom: height - BOTTOM_BAND,
  };
  return {
    plot,
    horizontal,
    vertical,
    px: value => plot.left + horizontal.share(value) * (plot.right - plot.left),
    py: value => plot.bottom - vertical.share(value) * (plot.bottom - plot.top),
  };
}

/** The frontier as a staircase: from the left edge across at the first point's y, down to each next. */
function staircase(
  frontier: readonly {readonly px: number; readonly py: number}[],
  plot: Plot,
): string {
  const [first] = frontier;
  if (first === undefined) return '';
  const steps = frontier.map((point, index) => {
    const next = frontier[index + 1];
    return `H${point.px} V${next === undefined ? plot.bottom : next.py}`;
  });
  return [`M${plot.left} ${first.py}`, ...steps].join(' ');
}

function roleOf(point: ParetoPoint, onFrontier: ReadonlySet<string>): Role {
  if (point.state === 'input') return 'input';
  if (point.state === 'failed') return 'failed';
  return onFrontier.has(point.id) ? 'frontier' : 'dominated';
}

const ROLE_WORD: Record<Role, string> = {
  input: 'Input',
  frontier: 'On the frontier',
  dominated: 'Dominated',
  failed: 'Failed',
};

/** The words a hover shows: the point's name and both of its values. */
function describe(point: ParetoPoint, x: ParetoAxis, y: ParetoAxis): string {
  const value = (axis: ParetoAxis, v: number): string => `${exact(v)} ${axis.unit}`.trim();
  return `${point.label ?? point.id} · ${value(x, point.x)} · ${value(y, point.y)}`;
}

/** An axis' title: its name and unit, and which way is better. */
function axisTitle(axis: ParetoAxis): string {
  const unit = axis.unit === '' ? '' : ` (${axis.unit})`;
  return `${axis.name}${unit}, ${axis.direction} is better`;
}

/**
 * The measured points of a two-objective search on a plane, with the Pareto frontier as a staircase.
 *
 * `cursor` is a time: only points measured by then are drawn, and the staircase is the frontier of
 * those, so moving the cursor along a time bar shows how the frontier advanced. (Without
 * a cursor every point is drawn.) The axes keep their range for the whole series, so nothing moves
 * but the points and the line. Both axes read the same way: toward the top and right is better
 * (an axis where lower is better is drawn backward), and each title says so. The input is a diamond;
 * the frontier's points are solid, the dominated ones muted, and the turned-down ones hollow.
 * Hovering a point names it and gives its values; `highlighted` ids are ringed.
 */
export function ParetoChart(props: {
  readonly label: string;
  readonly points: readonly ParetoPoint[];
  readonly x: ParetoAxis;
  readonly y: ParetoAxis;
  readonly cursor?: number;
  readonly highlighted?: readonly string[];
  readonly size?: ParetoSize;
  /** With it every point is a button that calls `pick(id)`; the points are one tab stop. */
  readonly pick?: (id: string) => void;
  /** Called with a point's id as the pointer or the focus enters it, and null as it leaves. */
  readonly hover?: (id: string | null) => void;
}): JSX.Element {
  const {points, x, y} = props;
  const height = HEIGHT[props.size ?? 'default'];
  const [hovered, setHovered] = useState<string | null>(null);
  const [measure, measured] = useInlineSize();
  const width = Math.max(MIN_WIDTH, measured ?? FALLBACK_WIDTH);
  const frame = frameFor(points, x, y, width, height);
  const {plot, px, py} = frame;

  const cursor = props.cursor ?? Number.POSITIVE_INFINITY;
  const seen = points.filter(p => p.time <= cursor);
  const competing = seen.filter(p => p.state !== 'failed');
  const frontier = frontierAt(competing, {x: x.direction, y: y.direction}, cursor);
  const onFrontier = new Set(frontier.map(p => p.id));
  // Left to right: the end with the worst x, which has the best y, first.
  const placed = [...frontier]
    .sort((a, b) => toward(a.x, x.direction) - toward(b.x, x.direction))
    .map(p => ({px: px(p.x), py: py(p.y)}));
  const lit = new Set(props.highlighted ?? []);
  // The hovered or ringed points are drawn last, so nothing covers them.
  const order = (p: ParetoPoint): number => (p.id === hovered ? 2 : lit.has(p.id) ? 1 : 0);
  const drawn = [...seen].sort((a, b) => order(a) - order(b));
  const named = seen.find(p => p.id === hovered);
  const roles = new Set(seen.map(p => roleOf(p, onFrontier)));
  const stop =
    (props.highlighted ?? []).find(id => seen.some(p => p.id === id)) ?? drawn.at(-1)?.id;
  const enter = (id: string | null): void => {
    setHovered(id);
    props.hover?.(id);
  };

  return (
    <div className="vs-pareto" ref={measure}>
      <svg
        className="vs-chart"
        viewBox={`0 0 ${width} ${height}`}
        role={props.pick === undefined ? 'img' : 'group'}
        aria-label={`${props.label}. ${frontier.length} of ${competing.length} passed points are on the frontier.`}
      >
        <Axes frame={frame} x={x} y={y} height={height} />
        {frontier.some(point => point.state !== 'input') ? (
          <path className="vs-pareto-frontier" d={staircase(placed, plot)} />
        ) : null}
        {drawn.map(point => (
          <PointMark
            key={point.id}
            point={point}
            role={roleOf(point, onFrontier)}
            at={{x: px(point.x), y: py(point.y)}}
            text={describe(point, x, y)}
            ringed={lit.has(point.id)}
            stop={point.id === stop}
            pick={props.pick}
            enter={enter}
          />
        ))}
        {named === undefined ? null : (
          <Hover text={describe(named, x, y)} at={{x: px(named.x), y: py(named.y)}} plot={plot} />
        )}
      </svg>
      <ul className="vs-dotlegend vs-serieslegend" aria-label="Points">
        {KEY_ORDER.map(role => (
          // A role not drawn yet keeps its place, unseen, so the legend is one height at every cursor.
          <li
            key={role}
            className="vs-dotlegend-item"
            {...(roles.has(role) ? {} : {'aria-hidden': true, 'data-absent': ''})}
          >
            <svg
              className="vs-pareto-key"
              data-role={role}
              viewBox="-10 -10 20 20"
              aria-hidden="true"
            >
              <g className="vs-pareto-point" data-role={role}>
                <Shape role={role} cx={0} cy={0} />
              </g>
            </svg>
            <Text variant="small" tone="muted">
              {ROLE_WORD[role]}
            </Text>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One point: its mark and ring, named by its title; a button when the chart can pick. */
function PointMark(props: {
  readonly point: ParetoPoint;
  readonly role: Role;
  readonly at: {readonly x: number; readonly y: number};
  readonly text: string;
  readonly ringed: boolean;
  readonly stop: boolean;
  readonly pick: ((id: string) => void) | undefined;
  readonly enter: (id: string | null) => void;
}): JSX.Element {
  const {point, role, at, pick, enter} = props;
  const body = (
    <>
      <title>{props.text}</title>
      {props.ringed ? <circle className="vs-chart-ring" cx={at.x} cy={at.y} r={R_RING} /> : null}
      <Shape role={role} cx={at.x} cy={at.y} />
    </>
  );
  if (pick === undefined) {
    return (
      // biome-ignore lint/a11y/noStaticElementInteractions: hover only names a point; the same words are its title and the chart's label.
      <g
        className="vs-pareto-point"
        data-role={role}
        onMouseEnter={() => enter(point.id)}
        onMouseLeave={() => enter(null)}
      >
        {body}
      </g>
    );
  }
  return (
    // biome-ignore lint/a11y/useSemanticElements: an SVG point cannot be a <button>; the group carries the button role, name and keys instead.
    <g
      className="vs-pareto-point vs-chart-pick"
      data-role={role}
      role="button"
      tabIndex={props.stop ? 0 : -1}
      aria-label={props.text}
      aria-pressed={props.ringed}
      onClick={() => pick(point.id)}
      onMouseEnter={() => enter(point.id)}
      onMouseLeave={() => enter(null)}
      onFocus={() => enter(point.id)}
      onBlur={() => enter(null)}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          pick(point.id);
        } else if (moveAmong(event.currentTarget, event.key)) {
          event.preventDefault();
        }
      }}
    >
      {body}
    </g>
  );
}

/** Moves the focus among a chart's points by an arrow, Home or End; whether `key` did. */
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

/** The grid, the tick labels, the two axes and their titles. */
function Axes(props: {
  readonly frame: Frame;
  readonly x: ParetoAxis;
  readonly y: ParetoAxis;
  readonly height: number;
}): JSX.Element {
  const {plot, horizontal, vertical, px, py} = props.frame;
  return (
    <>
      {vertical.ticks.map(tick => (
        <g key={`y${tick.value}`}>
          <line
            className="vs-chart-grid"
            x1={plot.left}
            x2={plot.right}
            y1={py(tick.value)}
            y2={py(tick.value)}
          />
          <text
            className="vs-chart-label"
            x={plot.left - LABEL_GAP}
            y={py(tick.value)}
            textAnchor="end"
            dominantBaseline="middle"
          >
            {tick.label}
          </text>
        </g>
      ))}
      {horizontal.ticks.map(tick => (
        <text
          key={`x${tick.value}`}
          className="vs-chart-label"
          x={px(tick.value)}
          y={plot.bottom + 18}
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
      <text className="vs-chart-label" x={plot.left} y={TOP_BAND - 12}>
        {`↑ ${axisTitle(props.y)}`}
      </text>
      <text className="vs-chart-label" x={plot.right} y={props.height - 6} textAnchor="end">
        {`${axisTitle(props.x)} →`}
      </text>
    </>
  );
}

/** A point's mark: a diamond for the input, a circle (larger on the frontier) for the rest. */
function Shape(props: {
  readonly role: Role;
  readonly cx: number;
  readonly cy: number;
}): JSX.Element {
  const {role, cx, cy} = props;
  return role === 'input' ? (
    <rect
      className="vs-pareto-mark"
      x={cx - R}
      y={cy - R}
      width={2 * R}
      height={2 * R}
      transform={`rotate(45 ${cx} ${cy})`}
    />
  ) : (
    <circle className="vs-pareto-mark" cx={cx} cy={cy} r={role === 'frontier' ? R : R_SMALL} />
  );
}

const HOVER_PAD = 8;
const HOVER_HEIGHT = 26;

/** A small label by a point, kept inside the plot. */
function Hover(props: {
  readonly text: string;
  readonly at: {readonly x: number; readonly y: number};
  readonly plot: Plot;
}): JSX.Element {
  const {text, at, plot} = props;
  const width = Math.ceil(text.length * GLYPH) + 2 * HOVER_PAD;
  const left = Math.max(plot.left, Math.min(plot.right - width, at.x - width / 2));
  const above = at.y - R_RING - HOVER_HEIGHT - 2 >= plot.top;
  const top = above ? at.y - R_RING - HOVER_HEIGHT - 2 : at.y + R_RING + 2;
  return (
    <g className="vs-pareto-hover" pointerEvents="none">
      <rect
        className="vs-pareto-hover-box"
        x={left}
        y={top}
        width={width}
        height={HOVER_HEIGHT}
        rx={4}
      />
      <text
        className="vs-pareto-hover-text"
        x={left + HOVER_PAD}
        y={top + HOVER_HEIGHT / 2}
        dominantBaseline="middle"
      >
        {text}
      </text>
    </g>
  );
}
