import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/** One layer of the stack: a value at each slice, in palette color number `series`. */
export interface AreaLayer {
  readonly key: string;
  readonly series: number;
  readonly values: readonly number[];
  /** Faded: another series is the one the reader follows. */
  readonly dim?: boolean;
}

const WIDTH = 1000;
const HEIGHT = 200;

/**
 * Layers stacked over a series of slices, each a filled area from the layer below to its own value
 * over the slices' centers; the height is drawn to the tallest stack. `titles` says what each
 * slice holds in full, shown when the pointer is over it (one title per slice). `label` names the
 * chart for assistive technology and `unit` says what the height measures; `from` and `to` word
 * the axis's two ends.
 */
export function StackedArea(props: {
  readonly label: string;
  readonly unit: string;
  readonly layers: readonly AreaLayer[];
  readonly titles: readonly string[];
  readonly from?: string;
  readonly to?: string;
}): JSX.Element {
  const count = props.titles.length;
  const stack = Array.from({length: count}, (_, at) =>
    props.layers.reduce((sum, layer) => sum + (layer.values[at] ?? 0), 0),
  );
  const tallest = Math.max(0, ...stack);
  const x = (at: number): number => (count <= 1 ? WIDTH / 2 : ((at + 0.5) / count) * WIDTH);
  const y = (value: number): number =>
    tallest === 0 ? HEIGHT : HEIGHT - (value / tallest) * HEIGHT;
  let below: number[] = new Array(count).fill(0);
  const paths = props.layers.map(layer => {
    const lower = below;
    const upper = lower.map((value, at) => value + (layer.values[at] ?? 0));
    below = upper;
    const top = upper.map((value, at) => `${x(at)} ${y(value)}`);
    const bottom = lower.map((value, at) => `${x(at)} ${y(value)}`).reverse();
    return {layer, d: count === 0 ? '' : `M${top.join(' L')} L${bottom.join(' L')} Z`};
  });
  return (
    <figure className="vs-bars">
      <svg
        className="vs-area"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${props.label}, in ${props.unit}`}
      >
        {paths.map(({layer, d}) => {
          const style: StyleWithVars = {'--seg-color': `var(--series-${layer.series})`};
          return (
            <path
              key={layer.key}
              className="vs-area-layer"
              data-dim={layer.dim === true || undefined}
              style={style}
              d={d}
            />
          );
        })}
        {props.titles.map((title, at) => (
          <rect
            key={title}
            className="vs-area-slice"
            x={(at / count) * WIDTH}
            y={0}
            width={WIDTH / count}
            height={HEIGHT}
          >
            <title>{title}</title>
          </rect>
        ))}
      </svg>
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
