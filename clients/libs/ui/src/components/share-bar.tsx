import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/** One part of the whole: a value, in palette color number `series`, and how it is worded. */
export interface SharePart {
  readonly key: string;
  readonly label: string;
  readonly value: number;
  /** The value as it is written beside the label. */
  readonly figure: string;
  readonly series: number;
}

/**
 * One bar cut into labelled parts, each as wide as its share of the whole, and under it a swatch,
 * a label and a figure for each part. A whole of nothing is an empty bar, and a part of nothing is
 * listed without a segment. `label` names the bar.
 */
export function ShareBar(props: {
  readonly label: string;
  readonly parts: readonly SharePart[];
}): JSX.Element {
  const whole = props.parts.reduce((total, part) => total + Math.max(0, part.value), 0);
  return (
    <figure className="vs-share" aria-label={props.label}>
      <div className="vs-share-bar" role="img" aria-label={props.label}>
        {props.parts.map(part => {
          const style: StyleWithVars = {
            '--seg-color': `var(--series-${part.series})`,
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
      <ul className="vs-share-parts">
        {props.parts.map(part => {
          const style: StyleWithVars = {'--seg-color': `var(--series-${part.series})`};
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
    </figure>
  );
}
