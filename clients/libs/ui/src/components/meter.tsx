import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

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

/**
 * One reading: its `label`, a meter of how full it is, and its `figure` in words. With no `share`
 * there is no reading, and the meter is empty, so a missing reading is never drawn as zero. `dim`
 * shows a reading that is no longer current: still there, and quieter. Its text stays legible (the
 * muted tone, not a fade), and its meter fades.
 */
export function Gauge(props: {
  readonly label: string;
  readonly share: number | null;
  readonly figure: string;
  readonly tone?: 'accent' | 'ok' | 'muted';
  readonly dim?: boolean;
}): JSX.Element {
  return (
    <span className="vs-gauge" data-dim={props.dim === true || undefined}>
      <Text variant="small" tone="muted">
        {props.label}
      </Text>
      <Meter
        inline
        label={`${props.label} ${props.figure}`}
        value={props.share ?? 0}
        tone={props.share === null ? 'muted' : (props.tone ?? 'ok')}
      />
      <Text variant="small" tone={props.dim === true ? 'muted' : 'default'}>
        {props.figure}
      </Text>
    </span>
  );
}
