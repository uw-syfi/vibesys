import type {JSX} from 'react';
import {MONO_ADVANCE, TYPE} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';
import {placeLabels, useInlineSize} from './label-fit.js';
import {niceScale} from './nice-scale.js';
import {Text} from './text.js';
import {fittedTimeTicks, type ZoneOffset} from './time-ticks.js';

/** One slice of a bar: `value` in the unit of the chart, in palette color number `series`. */
export interface BarSegment {
  readonly key: string;
  readonly series: number;
  readonly value: number;
  /** Faded: another series is the one the reader follows. */
  readonly dim?: boolean;
}

/** One bar: its segments stacked from the bottom, and what it stands for in words. */
export interface StackedBar {
  readonly id: string;
  readonly segments: readonly BarSegment[];
  readonly total: number;
  readonly title: string;
  /** With a `timeAxis`: when the bar's span begins, and how long it lasts, in milliseconds. */
  readonly startMs?: number;
  readonly widthMs?: number;
}

/**
 * A linear axis of time under the bars: each bar stands at its own `startMs` with the width of its own
 * `widthMs`, so a gap in the data is a gap in the plot, and the axis is ticked at tidy moments of the
 * clock `zone` tells (a day axis at its midnights), as many as the width holds, `label` writing each.
 */
export interface BarTimeAxis {
  readonly fromMs: number;
  readonly toMs: number;
  readonly label: (at: number, span: number, step: number) => string;
  readonly zone: ZoneOffset;
}

/** Where a bar stands on the plot, as shares of its width: its left edge and its width. */
export interface BarPlace {
  readonly left: number;
  readonly width: number;
}

/**
 * The places of the bars on an axis from `fromMs` to `toMs`: linear in time, so a bar's left edge is
 * its start's share of the span and its width is its duration's share, and two bars as far apart in
 * time are as far apart on the plot. A bar without a time is placed at the start, with no width.
 */
export function placeBars(
  bars: readonly StackedBar[],
  fromMs: number,
  toMs: number,
): readonly BarPlace[] {
  const span = toMs - fromMs;
  return bars.map(bar => ({
    left: span > 0 ? ((bar.startMs ?? fromMs) - fromMs) / span : 0,
    width: span > 0 ? (bar.widthMs ?? 0) / span : 0,
  }));
}

/** A label under the bar at index `at` (from 0). */
export interface BarMark {
  readonly at: number;
  readonly text: string;
}

/** The value axis has about this many steps: it reads at a glance and never crowds the plot. */
const SCALE_STEPS = 3;
/** The width of one glyph of the small face an axis label is set in. */
const SMALL_GLYPH = TYPE.small.size * MONO_ADVANCE;

function BarMarks(props: {
  readonly marks: readonly BarMark[];
  readonly count: number;
}): JSX.Element {
  const [measure, width] = useInlineSize();
  const share = (mark: BarMark): number => (mark.at + 0.5) / Math.max(1, props.count);
  // Once the row is measured only the labels that can be read side by side are drawn.
  const placed =
    width === null
      ? null
      : placeLabels(
          props.marks.map(mark => ({share: share(mark), text: mark.text})),
          width,
          SMALL_GLYPH,
        );
  return (
    <div ref={measure} className="vs-bars-marks" aria-hidden="true">
      {(placed ?? props.marks.map((_, index) => ({index, left: null}))).map(({index, left}) => {
        const mark = props.marks[index];
        if (mark === undefined) return null;
        const place: StyleWithVars =
          left === null
            ? {'--mark-at': `${(share(mark) * 100).toFixed(2)}%`}
            : {'--mark-left': `${left}px`};
        return (
          <span
            key={mark.at}
            className="vs-bars-mark"
            data-placed={left === null ? undefined : ''}
            style={place}
          >
            {mark.text}
          </span>
        );
      })}
    </div>
  );
}

/** The labels of a time axis, each centred under its moment on the plot. */
function TimeMarks(props: {
  readonly ticks: readonly {readonly at: number; readonly label: string; readonly share: string}[];
}): JSX.Element {
  return (
    <div className="vs-bars-marks" aria-hidden="true">
      {props.ticks.map(tick => {
        const place: StyleWithVars = {'--mark-at': tick.share};
        return (
          <span key={tick.at} className="vs-bars-mark" style={place}>
            {tick.label}
          </span>
        );
      })}
    </div>
  );
}

/** The value axis of a chart: its scale, the figure at each tidy step, and the same with the zero first. */
function valueAxis(props: {
  readonly bars: readonly StackedBar[];
  readonly valueFormat?: (value: number, step: number) => string;
  readonly zeroLabel?: string;
}): {
  readonly scale: ReturnType<typeof niceScale> | null;
  readonly labels: readonly {readonly tick: number; readonly text: string}[];
  readonly scaleLabels: readonly {readonly tick: number; readonly text: string}[];
  readonly top: number;
} {
  const tallest = Math.max(0, ...props.bars.map(bar => bar.total));
  const format = props.valueFormat;
  const scale = format === undefined ? null : niceScale(tallest, SCALE_STEPS);
  // The baseline is the axis itself, so zero gets no figure and no line.
  const steps = (scale?.ticks ?? []).filter(tick => tick > 0);
  // The axis steps are equal, so one step tells the formatter how many decimals every figure needs.
  const step = scale === null ? 0 : (scale.ticks[1] ?? scale.top);
  const labels = format === undefined ? [] : steps.map(tick => ({tick, text: format(tick, step)}));
  const scaleLabels =
    props.zeroLabel === undefined || scale === null
      ? labels
      : [{tick: 0, text: props.zeroLabel}, ...labels];
  return {scale, labels, scaleLabels, top: scale?.top ?? tallest};
}

/** Where each bar stands on a time axis, as styles, and the axis's ticks with the share of the plot each is at. */
function timedLayout(
  axis: BarTimeAxis,
  bars: readonly StackedBar[],
  plotWidth: number | null,
): {
  readonly places: readonly StyleWithVars[];
  readonly ticks: readonly {readonly at: number; readonly label: string; readonly share: string}[];
} {
  const span = axis.toMs - axis.fromMs;
  return {
    places: placeBars(bars, axis.fromMs, axis.toMs).map(
      (place): StyleWithVars => ({
        left: `${(place.left * 100).toFixed(3)}%`,
        width: `${(place.width * 100).toFixed(3)}%`,
      }),
    ),
    ticks: fittedTimeTicks(axis.fromMs, axis.toMs, plotWidth, axis.label, axis.zone).map(tick => ({
      at: tick.at,
      label: tick.label,
      share: `${(((tick.at - axis.fromMs) / span) * 100).toFixed(3)}%`,
    })),
  };
}

/**
 * Bars side by side over one axis, each a stack of colored segments, all drawn to the tallest
 * bar's total. `label` names the chart for assistive technology and `unit` says what the height
 * measures; `from` and `to` word the axis's two ends. A bar's `title` says what it holds in full.
 *
 * With `valueFormat` the chart draws a value axis, a hairline and a figure at each tidy step, and
 * draws the bars to its top instead of to the tallest bar; `format` words a value, given the
 * distance between two steps so that every figure of the axis has the same decimals. With `marks`
 * the bars they name are labelled underneath, as many as can be read side by side. A segment that
 * changes height eases to its new height.
 */
export function StackedBars(props: {
  readonly label: string;
  readonly unit: string;
  readonly bars: readonly StackedBar[];
  readonly from?: string;
  readonly to?: string;
  readonly valueFormat?: (value: number, step: number) => string;
  readonly marks?: readonly BarMark[];
  /** The bars on a linear time axis instead of side by side (see `BarTimeAxis`). */
  readonly timeAxis?: BarTimeAxis;
  /** The words of the axis's zero, written on the baseline (`$0`); without it the baseline has no figure. */
  readonly zeroLabel?: string;
  /**
   * The plot's height: the default, `tall` for a chart that is the point of its screen, or `compact`
   * for one panel among many (about 180 px with its labels).
   */
  readonly size?: 'tall' | 'compact';
}): JSX.Element {
  const [measurePlot, plotWidth] = useInlineSize();
  const {timeAxis} = props;
  const {scale, labels, scaleLabels, top} = valueAxis(props);
  const widest = Math.max(0, ...scaleLabels.map(label => label.text.length));
  const share = (value: number): string => `${((value / (top === 0 ? 1 : top)) * 100).toFixed(2)}%`;
  const figure: StyleWithVars = {
    '--bars-scale-chars': `${widest}ch`,
    '--bars-count': Math.max(1, props.bars.length),
  };
  const timed = timeAxis === undefined ? null : timedLayout(timeAxis, props.bars, plotWidth);
  const ticks = timed?.ticks ?? [];
  return (
    <figure
      className="vs-bars"
      data-scaled={
        scale !== null || props.marks !== undefined || timeAxis !== undefined || undefined
      }
      data-axis={scale !== null || undefined}
      data-size={props.size}
      style={figure}
    >
      {scale === null ? null : (
        <div className="vs-bars-scale" aria-hidden="true">
          {scaleLabels.map(label => {
            const place: StyleWithVars = {bottom: share(label.tick)};
            return (
              <span key={label.tick} className="vs-bars-tick" style={place}>
                {label.text}
              </span>
            );
          })}
        </div>
      )}
      <div
        className="vs-bars-plot"
        role="img"
        aria-label={`${props.label}, in ${props.unit}`}
        data-timed={timeAxis !== undefined || undefined}
        ref={timeAxis === undefined ? undefined : measurePlot}
      >
        {ticks.map(tick => {
          const place: StyleWithVars = {'--mark-at': tick.share};
          return <span key={tick.at} className="vs-bars-vline" style={place} aria-hidden="true" />;
        })}
        {labels.map(label => {
          const place: StyleWithVars = {bottom: share(label.tick)};
          return (
            <span key={label.tick} className="vs-bars-line" style={place} aria-hidden="true" />
          );
        })}
        {props.bars.map((bar, at) => (
          <div key={bar.id} className="vs-bars-bar" title={bar.title} style={timed?.places[at]}>
            {bar.segments.map(segment => {
              const style: StyleWithVars = {
                '--seg-color': `var(--series-${segment.series})`,
                height: `${top === 0 ? 0 : (segment.value / top) * 100}%`,
              };
              return (
                <span
                  key={segment.key}
                  className="vs-bars-segment"
                  data-dim={segment.dim === true || undefined}
                  style={style}
                />
              );
            })}
          </div>
        ))}
      </div>
      {timeAxis === undefined ? null : <TimeMarks ticks={ticks} />}
      {props.marks === undefined || timeAxis !== undefined ? null : (
        <BarMarks marks={props.marks} count={props.bars.length} />
      )}
      {props.from === undefined || props.to === undefined ? null : (
        <div className="vs-bars-axis">
          <Text variant="small" tone="muted">
            {props.from}
          </Text>
          <Text variant="small" tone="muted">
            {props.to}
          </Text>
        </div>
      )}
    </figure>
  );
}
