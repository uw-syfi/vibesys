import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';

/** A horizontal share of a whole (0 to 1) in a tone: a gain, a share of time. */
export function Meter(props: {
  readonly label: string;
  readonly value: number;
  readonly tone?: 'accent' | 'ok' | 'muted';
  /** The fill's color is palette color number `series` (`--series-N`) instead of a tone. */
  readonly series?: number;
  /** A fixed short track, to sit beside a figure in a row, instead of the width of its container. */
  readonly inline?: boolean;
}): JSX.Element {
  const share = Number.isFinite(props.value) ? Math.max(0, Math.min(1, props.value)) : 0;
  const fill: StyleWithVars = {
    '--meter-share': `${(share * 100).toFixed(1)}%`,
    ...(props.series === undefined ? {} : {'--meter-color': `var(--series-${props.series})`}),
  };
  return (
    <span
      className="vs-meter"
      role="img"
      aria-label={props.label}
      data-tone={props.tone ?? 'accent'}
      data-inline={props.inline === true || undefined}
      style={fill}
    >
      <span className="vs-meter-fill" />
    </span>
  );
}

/** Progress whose total is unknown. No numeric value is implied by the moving fill. */
export function LoadingBar(props: {readonly label: string}): JSX.Element {
  return (
    <span className="vs-meter" role="progressbar" aria-label={props.label}>
      <span className="vs-meter-fill vs-meter-indeterminate" />
    </span>
  );
}
