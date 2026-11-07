import type {JSX} from 'react';
import {Icon} from './icon.js';

/** One level of a drill-in path: the current level has no `press`. */
export interface Crumb {
  readonly id: string;
  readonly label: string;
  /** The label is a name (an idea's), drawn in the mono face names use everywhere. */
  readonly mono?: boolean;
  /** The step goes up and out of this trail, drawn as an arrow before its label. */
  readonly out?: boolean;
  readonly press?: () => void;
}

/**
 * Where the reader is: every zoom level from the top, each but the last a link back to it. The
 * last names what the panes show now. A `figure` after the last level (a result, such as a gain)
 * reads in the ok color, so the same line can also name a chain, such as the path to the best
 * result. The trail stays on one line: a step that is a name a person chose is cut with an ellipsis
 * (its whole text on hover) instead of the trail breaking between a step and the slash before it.
 */
export function Breadcrumb(props: {
  readonly label: string;
  readonly items: readonly Crumb[];
  readonly figure?: string;
}): JSX.Element {
  return (
    <nav className="vs-crumbs" aria-label={props.label}>
      <ol>
        {props.items.map((item, index) => (
          <li key={item.id}>
            {index === 0 ? null : (
              <span className="vs-crumb-sep" aria-hidden="true">
                /
              </span>
            )}
            {item.press === undefined ? (
              <span
                className="vs-crumb"
                data-mono={item.mono || undefined}
                title={item.label}
                aria-current="location"
              >
                {item.label}
              </span>
            ) : (
              <button
                type="button"
                className="vs-crumb"
                title={item.label}
                data-mono={item.mono || undefined}
                onClick={item.press}
              >
                {item.out ? <Icon name="chevron-left" /> : null}
                {item.label}
              </button>
            )}
          </li>
        ))}
      </ol>
      {props.figure === undefined ? null : <span className="vs-crumb-figure">{props.figure}</span>}
    </nav>
  );
}
