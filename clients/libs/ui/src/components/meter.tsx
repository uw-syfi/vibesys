import type {JSX, ReactNode} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/** A horizontal share of a whole (0 to 1) in a tone: a gain, a share of time. */
export function Meter(props: {
  readonly label: string;
  readonly value: number;
  readonly tone?: 'accent' | 'ok' | 'warn' | 'muted';
  /** The fill's color is palette color number `series` (`--series-N`) instead of a tone. */
  readonly series?: number;
  /** A fixed short track, to sit beside a figure in a row, instead of the width of its container. */
  readonly inline?: boolean;
  /** Drawn only: what it shows is said in words beside it, so assistive technology skips it. */
  readonly decorative?: boolean;
}): JSX.Element {
  const share = Number.isFinite(props.value) ? Math.max(0, Math.min(1, props.value)) : 0;
  const fill: StyleWithVars = {
    '--meter-share': `${(share * 100).toFixed(1)}%`,
    ...(props.series === undefined ? {} : {'--meter-color': `var(--series-${props.series})`}),
  };
  if (props.decorative === true) {
    return (
      <span
        className="vs-meter"
        aria-hidden="true"
        data-tone={props.tone ?? 'accent'}
        data-inline={props.inline === true || undefined}
        style={fill}
      >
        <span className="vs-meter-fill" />
      </span>
    );
  }
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
 * One reading: its `label`, a meter of how full it is, and its `figure` in words. It is announced
 * as one meter: the label, the share when there is one and the figure in words, so what is drawn
 * is not read three times. With no `share` there is no reading, and the meter is empty and
 * announced as unknown, so a missing reading is never drawn as zero. `dim` shows a reading that is
 * no longer current: still there, and quieter; `note` says why to a listener ("stale 42 s"). Its
 * text stays legible (the muted tone, not a fade), and its meter fades.
 *
 * `figureWidth` is the width, in characters, of the widest figure this reading will show. The
 * figure is set in tabular numerals and takes at least that room, so a figure that gains a digit
 * moves nothing beside it.
 */
export function Gauge(props: {
  readonly label: string;
  readonly share: number | null;
  readonly figure: string;
  readonly figureWidth: number;
  readonly tone?: 'ok' | 'warn';
  readonly dim?: boolean;
  readonly note?: string;
}): JSX.Element {
  const known = props.share !== null && Number.isFinite(props.share);
  const share = known ? Math.max(0, Math.min(1, props.share ?? 0)) : null;
  const spoken = share === null ? 'unknown' : props.figure;
  const figure: StyleWithVars = {'--gauge-figure-width': `${props.figureWidth}ch`};
  return (
    // biome-ignore lint/a11y/useSemanticElements: a native <meter> draws its own shadow tree and shows its children only as a fallback, so the gauge's label, bar and figure could not be its content; the ARIA meter pattern on a span names the same thing. Alternatives: style <meter> with its pseudo-elements (loses the bar's tones, track and fade, and differs by engine), or drop the role (the value would be text only, which is what this replaces).
    <span
      className="vs-gauge"
      role="meter"
      aria-label={props.label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={share === null ? undefined : Math.round(share * 100)}
      aria-valuetext={props.note === undefined ? spoken : `${spoken}, ${props.note}`}
      data-dim={props.dim === true || undefined}
    >
      <Text variant="small" tone="muted">
        {props.label}
      </Text>
      <Meter
        inline
        decorative
        label={props.label}
        value={share ?? 0}
        tone={share === null ? 'muted' : (props.tone ?? 'ok')}
      />
      <span className="vs-gauge-figure" style={figure}>
        <Text variant="small" tone={props.dim === true ? 'muted' : 'default'}>
          {props.figure}
        </Text>
      </span>
    </span>
  );
}

/**
 * A row of readings that is one line high whether it holds any or not, so the surface around it
 * keeps its height when its readings come and go.
 */
export function Readings(props: {readonly children?: ReactNode}): JSX.Element {
  return <div className="vs-readings">{props.children}</div>;
}
