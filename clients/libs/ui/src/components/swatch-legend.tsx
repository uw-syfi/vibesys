import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/**
 * A small square of the palette's color number `series` (`--series-N`), or, `hatched`, drawn with
 * the hatching a Gantt gives such a piece (muted when it has no series), or `muted`, the flat grey
 * of a Gantt piece that is no work. Decorative: a name always sits beside it.
 */
export function Swatch(props: {
  readonly series?: number;
  readonly hatched?: boolean;
  readonly muted?: boolean;
}): JSX.Element {
  const color: StyleWithVars =
    props.series !== undefined
      ? {'--seg-color': `var(--series-${props.series})`}
      : props.muted === true
        ? {'--seg-color': 'var(--muted)'}
        : {};
  return (
    <span
      className="vs-series-swatch"
      data-hatched={props.hatched === true || undefined}
      style={color}
      aria-hidden="true"
    />
  );
}

/** One entry of a `SwatchLegend`. */
export interface SwatchItem {
  readonly label: string;
  readonly series?: number;
  readonly hatched?: boolean;
  /** Flat grey, like a Gantt piece that is no work. */
  readonly muted?: boolean;
  /** The label is an identifier a person may copy (a model id), set in the code face. */
  readonly mono?: boolean;
}

/** A one-line key to colored bars: each swatch and its name, named as a list by `label`. */
export function SwatchLegend(props: {
  readonly label: string;
  readonly items: readonly SwatchItem[];
}): JSX.Element {
  return (
    <ul className="vs-dotlegend vs-serieslegend" aria-label={props.label}>
      {props.items.map(item => (
        <li key={item.label} className="vs-dotlegend-item">
          <Swatch
            {...(item.series === undefined ? {} : {series: item.series})}
            {...(item.hatched === true ? {hatched: true} : {})}
            {...(item.muted === true ? {muted: true} : {})}
          />
          <Text variant={item.mono === true ? 'code' : 'small'} tone="muted">
            {item.label}
          </Text>
        </li>
      ))}
    </ul>
  );
}
