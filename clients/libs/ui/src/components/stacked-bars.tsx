import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
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

/**
 * Bars side by side over one axis, each a stack of colored segments, all drawn to the tallest
 * bar's total. `label` names the chart for assistive technology and `unit` says what the height
 * measures; `from` and `to` word the axis's two ends. A bar's `title` says what it holds in full.
 */
export function StackedBars(props: {
  readonly label: string;
  readonly unit: string;
  readonly bars: readonly StackedBar[];
  readonly from?: string;
  readonly to?: string;
}): JSX.Element {
  const tallest = Math.max(0, ...props.bars.map(bar => bar.total));
  return (
    <figure className="vs-bars">
      <div className="vs-bars-plot" role="img" aria-label={`${props.label}, in ${props.unit}`}>
        {props.bars.map(bar => (
          <div key={bar.id} className="vs-bars-bar" title={bar.title}>
            {bar.segments.map(segment => {
              const style: StyleWithVars = {
                '--seg-color': `var(--series-${segment.series})`,
                height: `${tallest === 0 ? 0 : (segment.value / tallest) * 100}%`,
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
