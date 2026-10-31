import {type JSX, type ReactNode, useCallback, useId} from 'react';
import {Text} from './text.js';

/** Panes side by side, filling the screen's body. */
export function Panes(props: {readonly children: ReactNode}): JSX.Element {
  return <div className="vs-panes">{props.children}</div>;
}

/**
 * A pane's width: `rail` (a collapsed parent), `list` (the list a selection was made in), `outline`
 * (a list of short rows beside what one of them opened: narrower than a `list`), `fill` (what was
 * selected) or `inspector` (one item read in full beside its list).
 */
export type PaneWidth = 'rail' | 'list' | 'outline' | 'master' | 'fill' | 'inspector';

/**
 * A titled region of the frame whose body scrolls on its own. `kicker` says what kind of thing it
 * shows, `title` which one and `meta` its facts, all on one line in a compact screen. A pane given
 * a new `enterKey` slides in and takes the focus on its heading (the selection opened it);
 * `focusHeading` false leaves the focus where it was (a live update). Reduced motion removes the
 * slide and the flash.
 */
export function Pane(props: {
  readonly kicker: string;
  readonly title: string;
  readonly width: PaneWidth;
  readonly meta?: ReactNode;
  readonly actions?: ReactNode;
  readonly enterKey?: string;
  readonly focusHeading?: boolean;
  /** The body does not scroll: its view scrolls its own list, under what stays above it. */
  readonly fixed?: boolean;
  /** The title is a name (an idea's), drawn in the mono face names use everywhere. */
  readonly mono?: boolean;
  /**
   * No header: the body brings its own controls (tabs, filters), and `kicker` and `title` only name
   * the region for assistive technology.
   */
  readonly bare?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const headingId = useId();
  const {enterKey, focusHeading = true} = props;
  // The section is keyed by `enterKey`, so its heading mounts once per opening; this callback
  // changes only with the key (and `focusHeading`), so it fires once then and not on every render.
  const focusOnOpen = useCallback(
    (heading: HTMLHeadingElement | null) => {
      if (heading !== null && enterKey !== undefined && focusHeading) {
        heading.focus({preventScroll: true});
      }
    },
    [enterKey, focusHeading],
  );
  return (
    <section
      className="vs-pane"
      data-width={props.width}
      data-entering={enterKey === undefined ? undefined : ''}
      key={enterKey}
      aria-labelledby={props.bare === true ? undefined : headingId}
      aria-label={props.bare === true ? `${props.kicker} ${props.title}` : undefined}
    >
      {props.bare === true ? null : (
      <header className="vs-pane-header">
        <div className="vs-pane-titles">
          <Text variant="section">{props.kicker}</Text>
          <h2
            className="vs-pane-title"
            data-mono={props.mono || undefined}
            id={headingId}
            ref={focusOnOpen}
            tabIndex={-1}
          >
            {props.title}
          </h2>
          {props.meta === undefined ? null : <div className="vs-pane-meta">{props.meta}</div>}
        </div>
        {props.actions === undefined ? null : (
          <div className="vs-pane-actions">{props.actions}</div>
        )}
      </header>
      )}
      <div className="vs-pane-body" data-fixed={props.fixed || undefined}>
        {props.children}
      </div>
    </section>
  );
}
