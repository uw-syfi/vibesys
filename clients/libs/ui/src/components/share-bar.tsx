import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
import type {StatusTone} from './status.js';
import {Text} from './text.js';

/** The color a status tone is drawn in, as its dot is drawn. */
const TONE_COLOR: Readonly<Record<StatusTone, string>> = {
  neutral: 'var(--muted)',
  live: 'var(--accent)',
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  err: 'var(--err)',
};

/** One part of the whole: a value, in palette color number `series`, and how it is worded. */
export interface SharePart {
  readonly key: string;
  readonly label: string;
  readonly value: number;
  /** The value as it is written beside the label. */
  readonly figure: string;
  readonly series: number;
  /** Drawn in the color of this status tone (as its dot is), instead of palette color `series`. */
  readonly tone?: StatusTone;
}

const colorOf = (part: SharePart): string =>
  part.tone === undefined ? `var(--series-${part.series})` : TONE_COLOR[part.tone];

/**
 * One bar cut into labelled parts, each as wide as its share of the whole, and under it a swatch,
 * a label and a figure for each part. A whole of nothing is an empty bar, and a part of nothing is
 * listed without a segment. `label` names the bar; `bare` leaves the list out.
 */
export function ShareBar(props: {
  readonly label: string;
  readonly parts: readonly SharePart[];
  /** The bar alone, for a bar in a table row: each segment still says its part on hover. */
  readonly bare?: boolean;
}): JSX.Element {
  const whole = props.parts.reduce((total, part) => total + Math.max(0, part.value), 0);
  return (
    <figure
      className="vs-share"
      data-bare={props.bare === true || undefined}
      aria-label={props.label}
    >
      <div className="vs-share-bar" role="img" aria-label={props.label}>
        {props.parts.map(part => {
          const style: StyleWithVars = {
            '--seg-color': colorOf(part),
            flexGrow: whole === 0 ? 0 : Math.max(0, part.value) / whole,
          };
          return part.value > 0 ? (
            <span
              key={part.key}
              className="vs-share-segment"
              style={style}
              title={`${part.label} ${part.figure}`}
            />
          ) : null;
        })}
      </div>
      {props.bare === true ? null : (
        <ul className="vs-share-parts">
          {props.parts.map(part => {
            const style: StyleWithVars = {'--seg-color': colorOf(part)};
            return (
              <li key={part.key} className="vs-share-part">
                <span className="vs-share-swatch" style={style} aria-hidden="true" />
                <Text variant="small" tone="muted">
                  {part.label}
                </Text>
                <Text variant="small">{part.figure}</Text>
              </li>
            );
          })}
        </ul>
      )}
    </figure>
  );
}
