import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';

/** A horizontal share of a whole (0 to 1) in a tone: a gain, a share of time. */
export function Meter(props: {
  readonly label: string;
  readonly value: number;
  readonly tone?: 'accent' | 'ok' | 'muted';
}): JSX.Element {
  const share = Number.isFinite(props.value) ? Math.max(0, Math.min(1, props.value)) : 0;
  const fill: StyleWithVars = {'--meter-share': `${(share * 100).toFixed(1)}%`};
  return (
    <span
      className="vs-meter"
      role="img"
      aria-label={props.label}
      data-tone={props.tone ?? 'accent'}
      style={fill}
    >
      <span className="vs-meter-fill" />
    </span>
  );
}
