import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {useInlineSize} from './label-fit.js';
import {niceScale} from './nice-scale.js';

/** One overlaid layer: a value at each slice, in palette color number `series`. */
export interface ChartLayer {
  readonly key: string;
  /** What the layer is called in the tooltip. */
  readonly name: string;
  readonly series: number;
  readonly values: readonly number[];
}

const FALLBACK_WIDTH = 1000;
const MIN_WIDTH = 240;
const HEIGHT = 220;
const PLOT_TOP = 12;
const X_BAND = 28;
const MARGIN_RIGHT = 10;
const LABEL_GAP = 8;
const EDGE = 4;
const GLYPH = 7.2;
const TICKS = 4;
const TIP_ROW = 16;
const TIP_PAD = 8;

/** Where the plot sits in the chart and where a slice and a value fall in it. */
interface Frame {
  readonly left: number;
  readonly right: number;
  readonly bottom: number;
  readonly count: number;
  readonly x: (at: number) => number;
  readonly y: (value: number) => number;
}

function Axes(props: {
  readonly frame: Frame;
  readonly ticks: readonly {readonly tick: number; readonly text: string}[];
  readonly labels: readonly string[];
}): JSX.Element {
  const {frame, ticks, labels} = props;
  const {left, right, bottom} = frame;
  const every = Math.max(1, Math.ceil((frame.count * 64) / Math.max(1, right - left)));
  return (
    <>
      {ticks.map(({tick, text}) => (
        <g key={tick}>
          <line
            className="vs-chart-grid"
            x1={left}
            x2={right}
            y1={frame.y(tick)}
            y2={frame.y(tick)}
          />
          <text
            className="vs-chart-label"
            x={left - LABEL_GAP}
            y={frame.y(tick)}
            textAnchor="end"
            dominantBaseline="middle"
          >
            {text}
          </text>
        </g>
      ))}
      <line className="vs-chart-axis" x1={left} x2={left} y1={PLOT_TOP} y2={bottom} />
      <line className="vs-chart-axis" x1={left} x2={right} y1={bottom} y2={bottom} />
      {labels.map((text, at) =>
        at % every === 0 ? (
          <text
            key={frame.x(at)}
            className="vs-chart-label"
            x={frame.x(at)}
            y={bottom + 16}
            textAnchor="middle"
          >
            {text}
          </text>
        ) : null,
      )}
    </>
  );
}

/** One slice: the band the pointer hits, the line through it, and the tooltip of each layer's value. */
function Slice(props: {
  readonly frame: Frame;
  readonly at: number;
  readonly text: string;
  readonly layers: readonly ChartLayer[];
  readonly format: (value: number) => string;
}): JSX.Element {
  const {frame, at, text, layers} = props;
  const {right, bottom} = frame;
  const step = (right - frame.left) / Math.max(1, frame.count);
  const rows = layers.map(layer => ({
    layer,
    text: `${layer.name} ${props.format(layer.values[at] ?? 0)}`,
  }));
  const wide = Math.max(text.length, ...rows.map(row => row.text.length)) * GLYPH + 2 * TIP_PAD;
  const height = (rows.length + 1) * TIP_ROW + TIP_PAD;
  const here = frame.x(at);
  const tipX = here + wide + TIP_PAD > right ? here - wide - TIP_PAD : here + TIP_PAD;
  return (
    <g className="vs-area-chart-slice">
      <rect
        className="vs-area-chart-hit"
        x={here - step / 2}
        y={PLOT_TOP}
        width={step}
        height={bottom - PLOT_TOP}
      />
      <line className="vs-area-chart-cross" x1={here} x2={here} y1={PLOT_TOP} y2={bottom} />
      <g className="vs-area-chart-tip">
        <rect
          className="vs-area-chart-tip-box"
          x={tipX}
          y={PLOT_TOP}
          width={wide}
          height={height}
        />
        <text className="vs-chart-label" x={tipX + TIP_PAD} y={PLOT_TOP + TIP_ROW}>
          {text}
        </text>
        {rows.map((row, index) => (
          <text
            key={row.layer.key}
            className="vs-area-chart-tip-row"
            style={{'--seg-color': `var(--series-${row.layer.series})`} as StyleWithVars}
            x={tipX + TIP_PAD}
            y={PLOT_TOP + TIP_ROW * (index + 2)}
          >
            {row.text}
          </text>
        ))}
      </g>
    </g>
  );
}

/**
 * Layers overlaid over a series of slices, each a filled area with its line, drawn smoothly
 * through the slices' centers on a tidy value axis from zero. `labels` says what each slice is;
 * the horizontal axis names some of them. Pointing at a slice draws a line through it and a
 * tooltip of each layer's value there, with no state: the slices are shapes the page's stylesheet
 * reveals under the pointer. `format` words a value, on the axis and in the tooltip; `label` names
 * the chart for assistive technology and `unit` says what the height measures.
 */
export function AreaChart(props: {
  readonly label: string;
  readonly unit: string;
  readonly labels: readonly string[];
  readonly layers: readonly ChartLayer[];
  readonly format: (value: number) => string;
}): JSX.Element {
  const [measure, measured] = useInlineSize();
  const width = Math.max(MIN_WIDTH, measured ?? FALLBACK_WIDTH);
  const count = props.labels.length;
  const scale = niceScale(Math.max(0, ...props.layers.flatMap(layer => layer.values)), TICKS);
  const ticks = scale.ticks.map(tick => ({tick, text: props.format(tick)}));
  const left =
    EDGE + Math.ceil(Math.max(1, ...ticks.map(each => each.text.length)) * GLYPH) + LABEL_GAP;
  const right = width - MARGIN_RIGHT;
  const bottom = HEIGHT - X_BAND;
  const frame: Frame = {
    left,
    right,
    bottom,
    count,
    x: at => left + ((at + 0.5) / Math.max(1, count)) * (right - left),
    y: value => bottom - (value / scale.top) * (bottom - PLOT_TOP),
  };
  return (
    <figure ref={measure} className="vs-area-chart">
      <svg
        className="vs-area-chart-plot"
        width={width}
        height={HEIGHT}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        role="img"
        aria-label={`${props.label}, in ${props.unit}`}
      >
        <Axes frame={frame} ticks={ticks} labels={props.labels} />
        {props.layers.map(layer => {
          const style: StyleWithVars = {'--seg-color': `var(--series-${layer.series})`};
          const points = layer.values.map((value, at) => ({x: frame.x(at), y: frame.y(value)}));
          return (
            <g key={layer.key} className="vs-area-chart-layer" style={style}>
              <path className="vs-area-chart-fill" d={areaPath(points, bottom)} />
              <path className="vs-area-chart-line" d={linePath(points)} />
            </g>
          );
        })}
        {props.labels.map((text, at) => (
          <Slice
            key={frame.x(at)}
            frame={frame}
            at={at}
            text={text}
            layers={props.layers}
            format={props.format}
          />
        ))}
      </svg>
    </figure>
  );
}

interface Point {
  readonly x: number;
  readonly y: number;
}

/** A smooth path through `points` that never overshoots between them (a monotone cubic). */
export function linePath(points: readonly Point[]): string {
  const first = points[0];
  if (first === undefined) return '';
  if (points.length === 1) return `M${first.x} ${first.y}`;
  const slopes: number[] = [];
  for (let at = 0; at < points.length - 1; at += 1) {
    const from = points[at] as Point;
    const to = points[at + 1] as Point;
    slopes.push((to.y - from.y) / (to.x - from.x || 1));
  }
  const tangents = points.map((_, at) => {
    const before = slopes[at - 1];
    const after = slopes[at];
    if (before === undefined) return after ?? 0;
    if (after === undefined) return before;
    return before * after <= 0 ? 0 : (before + after) / 2;
  });
  // Clamp the tangents so a segment is monotone (Fritsch-Carlson).
  for (let at = 0; at < slopes.length; at += 1) {
    const slope = slopes[at] ?? 0;
    if (slope === 0) {
      tangents[at] = 0;
      tangents[at + 1] = 0;
      continue;
    }
    const a = (tangents[at] ?? 0) / slope;
    const b = (tangents[at + 1] ?? 0) / slope;
    const size = Math.hypot(a, b);
    if (size > 3) {
      tangents[at] = ((3 * a) / size) * slope;
      tangents[at + 1] = ((3 * b) / size) * slope;
    }
  }
  let path = `M${first.x} ${first.y}`;
  for (let at = 0; at < points.length - 1; at += 1) {
    const from = points[at] as Point;
    const to = points[at + 1] as Point;
    const reach = (to.x - from.x) / 3;
    path += ` C${from.x + reach} ${from.y + reach * (tangents[at] ?? 0)} ${to.x - reach} ${to.y - reach * (tangents[at + 1] ?? 0)} ${to.x} ${to.y}`;
  }
  return path;
}

/** The area under `linePath(points)` down to `bottom`. */
export function areaPath(points: readonly Point[], bottom: number): string {
  const first = points[0];
  const last = points.at(-1);
  if (first === undefined || last === undefined) return '';
  return `${linePath(points)} L${last.x} ${bottom} L${first.x} ${bottom} Z`;
}
