import type {JSX, ReactNode} from 'react';
import {Icon} from './icon.js';
import type {PageState} from './layout.js';

/** How much a screen's parts are spaced: `comfortable` (default) or `compact` (dense rows, slim headers). */
export type Density = 'comfortable' | 'compact';

/**
 * A screen that never scrolls as a whole: a fixed `header` (title, status, actions) above a body
 * that fills the rest of its container. Whatever is long scrolls inside a `Pane`. `density` is
 * read by the components inside (panes, pick rows, the scrubber), so a screen needs no overrides.
 * The header is the screen's header row for the window's toasts, which sit just below it. As the
 * page's main landmark it says whether what it shows has settled (`state`, as `data-state`, as
 * `Page` does); one inside another says nothing, the outer one does.
 */
export function Screen(
  props: {
    readonly header: ReactNode;
    readonly density?: Density;
    readonly children: ReactNode;
  } & (
    | {readonly landmark?: 'main'; readonly state: PageState}
    | {readonly landmark: 'none'; readonly state?: never}
  ),
): JSX.Element {
  const Root = props.landmark === 'none' ? 'div' : 'main';
  return (
    <Root
      className="vs-screen"
      data-density={props.density ?? 'comfortable'}
      data-state={props.state}
    >
      <div className="vs-screen-header">{props.header}</div>
      <div className="vs-screen-body">{props.children}</div>
    </Root>
  );
}

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
 * result.
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
              <span className="vs-crumb" data-mono={item.mono || undefined} aria-current="location">
                {item.label}
              </span>
            ) : (
              <button
                type="button"
                className="vs-crumb"
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
