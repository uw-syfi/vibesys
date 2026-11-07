import type {JSX, ReactNode} from 'react';
import type {PageState} from './layout.js';
import {ScrollArea} from './scroll-area.js';

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
      <ScrollArea kind="column" className="vs-screen-body">
        {props.children}
      </ScrollArea>
    </Root>
  );
}
