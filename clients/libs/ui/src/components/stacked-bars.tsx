import type {JSX} from 'react';
import {MONO_ADVANCE, TYPE} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';
import {placeLabels, useInlineSize} from './label-fit.js';
import {niceScale} from './nice-scale.js';
import {Text} from './text.js';

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
  /** The plot's height: the default, or `tall` for a chart that is the point of its screen. */
  readonly size?: 'tall';
}): JSX.Element {
  const tallest = Math.max(0, ...props.bars.map(bar => bar.total));
  const format = props.valueFormat;
  const scale = format === undefined ? null : niceScale(tallest, SCALE_STEPS);
  const top = scale?.top ?? tallest;
  // The baseline is the axis itself, so zero gets no figure and no line.
  const steps = (scale?.ticks ?? []).filter(tick => tick > 0);
  // The axis steps are equal, so one step tells the formatter how many decimals every figure needs.
  const step = scale === null ? 0 : (scale.ticks[1] ?? scale.top);
  const labels = format === undefined ? [] : steps.map(tick => ({tick, text: format(tick, step)}));
  const widest = Math.max(0, ...labels.map(label => label.text.length));
  const share = (value: number): string => `${((value / (top === 0 ? 1 : top)) * 100).toFixed(2)}%`;
  const figure: StyleWithVars = {
    '--bars-scale-chars': `${widest}ch`,
    '--bars-count': Math.max(1, props.bars.length),
  };
  return (
    <figure
      className="vs-bars"
      data-scaled={scale !== null || props.marks !== undefined || undefined}
      data-axis={scale !== null || undefined}
      data-size={props.size}
      style={figure}
    >
      {scale === null ? null : (
        <div className="vs-bars-scale" aria-hidden="true">
          {labels.map(label => {
            const place: StyleWithVars = {bottom: share(label.tick)};
            return (
              <span key={label.tick} className="vs-bars-tick" style={place}>
                {label.text}
              </span>
            );
          })}
        </div>
      )}
      <div className="vs-bars-plot" role="img" aria-label={`${props.label}, in ${props.unit}`}>
        {labels.map(label => {
          const place: StyleWithVars = {bottom: share(label.tick)};
          return (
            <span key={label.tick} className="vs-bars-line" style={place} aria-hidden="true" />
          );
        })}
        {props.bars.map(bar => (
          <div key={bar.id} className="vs-bars-bar" title={bar.title}>
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
      {props.marks === undefined ? null : (
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
