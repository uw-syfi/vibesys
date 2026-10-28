import type {JSX} from 'react';
import {Text} from './text.js';

const WIDTH = 200;
const HEIGHT = 36;
const PAD = 3;

/** One step of a series: the value from `x` on (until the next step). */
export interface SparkStep {
  readonly x: number;
  readonly y: number;
}

/**
 * A tiny best-so-far line with no axes: `steps` drawn as a step line that holds each value until
 * the next, over the x range `[from, to]`, and an optional dashed `reference` line (the starting
 * value). The y range fits the steps and the reference, so a flat series sits in the middle.
 * `label` describes it for assistive technology.
 */
export function Sparkline(props: {
  readonly label: string;
  readonly steps: readonly SparkStep[];
  readonly from: number;
  readonly to: number;
  readonly reference?: number;
}): JSX.Element {
  const ys = [
    ...props.steps.map(step => step.y),
    ...(props.reference === undefined ? [] : [props.reference]),
  ];
  const low = Math.min(...ys);
  const high = Math.max(...ys);
  const span = high - low;
  const y = (value: number): number =>
    span === 0 ? HEIGHT / 2 : HEIGHT - PAD - ((value - low) / span) * (HEIGHT - 2 * PAD);
  const width = props.to - props.from;
  const x = (value: number): number =>
    width <= 0 ? 0 : Math.max(0, Math.min(WIDTH, ((value - props.from) / width) * WIDTH));
  const first = props.steps[0];
  const path =
    first === undefined
      ? ''
      : [
          `M${x(first.x)} ${y(first.y)}`,
          ...props.steps.slice(1).map(step => `H${x(step.x)} V${y(step.y)}`),
          `H${WIDTH}`,
        ].join(' ');
  return (
    <svg
      className="vs-spark"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={props.label}
    >
      {props.reference === undefined ? null : (
        <line
          className="vs-spark-reference"
          x1={0}
          x2={WIDTH}
          y1={y(props.reference)}
          y2={y(props.reference)}
        />
      )}
      {path === '' ? null : <path className="vs-spark-line" d={path} />}
    </svg>
  );
}

/** One entry of a `SparklineKey`: the best-so-far `line` or the dashed `reference`, and its word. */
export interface SparklineKeyItem {
  readonly mark: 'line' | 'reference';
  readonly label: string;
}

/** A one-line key to what a `Sparkline` draws: its line and its reference, each beside its word. */
export function SparklineKey(props: {
  readonly label: string;
  readonly items: readonly SparklineKeyItem[];
}): JSX.Element {
  return (
    <ul className="vs-dotlegend vs-spark-key" aria-label={props.label}>
      {props.items.map(item => (
        <li key={item.label} className="vs-dotlegend-item">
          <span className="vs-spark-key-mark" data-mark={item.mark} aria-hidden="true" />
          <Text variant="small" tone="muted">
            {item.label}
          </Text>
        </li>
      ))}
    </ul>
  );
}
