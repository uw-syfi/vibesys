import {
  type CSSProperties,
  createContext,
  type JSX,
  type ReactNode,
  useCallback,
  useContext,
  useId,
  useState,
} from 'react';
import type {SpaceToken} from '../tokens/index.js';
import {Breadcrumb, type Crumb} from './breadcrumb.js';
import {Icon} from './icon.js';
import {ScrollArea} from './scroll-area.js';

/** The page content's max width: `content` (1060), `wide` (1100) or `narrow` (900). */
export type PageWidth = 'content' | 'wide' | 'narrow' | 'fill';

/**
 * Whether what a screen shows has settled: `loading` while a reply it asked for is outstanding (the
 * reply may change what it shows), `ready` once none is. A screen carries it as `data-state` on its
 * page, so a test or a script waits for a state, not for a duration.
 */
export type PageState = 'loading' | 'ready';

/**
 * What the window does when a page is drawn: called with the page's `<main>` once, as it arrives.
 * The window that holds the pages provides it (where the focus goes when a screen opens or is left),
 * so a page is a plain component that knows nothing of how the window moves the focus. With none
 * provided, nothing happens.
 */
const PageArrival = createContext<(page: HTMLElement) => void>(() => undefined);

/** The context a window provides its arrival through: `pageArrival.Provider` around its pages. */
export const pageArrival = PageArrival;

/** `loading` while `awaiting`, else `ready`: the one place the two words are chosen. */
export function pageState(awaiting: boolean): PageState {
  return awaiting ? 'loading' : 'ready';
}
export type Align = 'start' | 'center' | 'end' | 'stretch' | 'baseline';
export type Justify = 'start' | 'center' | 'end' | 'between';
/**
 * The narrowest column a `Grid` wraps at: a form field, a tile, a theme swatch, or a theme swatch
 * of a group of three that stays in one row down to the narrowest window.
 */
export type GridColumn = 'field' | 'tile' | 'swatch' | 'swatch-row';

interface FlexProps {
  readonly gap?: SpaceToken;
  /** The gap between the lines of a wrapped row, when it is not the gap inside a line. */
  readonly rowGap?: SpaceToken;
  readonly align?: Align;
  readonly justify?: Justify;
  readonly wrap?: boolean;
  /** Take the free space of the `Row` or `Stack` this sits in. */
  readonly grow?: boolean;
  /**
   * Set apart from the groups around it by a step more than the gap they keep, so that the gap
   * between this group and its neighbours is wider than the gap inside it, and a reader sees where
   * it begins and ends.
   */
  readonly apart?: boolean;
  /**
   * A fixed width, whatever the content: a figure's block (`figure`), so what shares its row never moves; or
   * a summary (`summary`), a block of a few lines beside a chart, as wide as a name and a figure need; or
   * a chart (`chart`), which takes the rest of its row and drops to a row of its own, full width, when the
   * row leaves it less than a readable width; or a column of text to be read (`reading`), as wide as
   * a page of text reads well and centred in what holds it.
   */
  readonly width?: 'figure' | 'summary' | 'chart' | 'reading';
  readonly children: ReactNode;
}

function gapStyle(gap: SpaceToken, rowGap?: SpaceToken): CSSProperties {
  return rowGap === undefined
    ? {gap: `var(--${gap})`}
    : {columnGap: `var(--${gap})`, rowGap: `var(--${rowGap})`};
}

function Flex(props: FlexProps & {readonly direction: 'row' | 'column'}): JSX.Element {
  const {direction, gap = 'space-3', align, justify, wrap = false, grow = false} = props;
  const {apart = false} = props;
  return (
    <div
      className="vs-flex"
      data-direction={direction}
      data-align={align}
      data-justify={justify}
      data-wrap={wrap || undefined}
      data-grow={grow || undefined}
      data-apart={apart || undefined}
      data-width={props.width}
      style={gapStyle(gap, props.rowGap)}
    >
      {props.children}
    </div>
  );
}

/**
 * The room `children` would take, kept empty: it is invisible, unreachable and unannounced. A line
 * that appears and disappears (a state's name before there is a state) is drawn here while absent,
 * so what shares its column never moves when it comes. `collapseInNarrowWindow` gives that room back
 * below the app's narrow-window breakpoint, where a wrapped invisible item would make an empty row;
 * the real child still appears normally when the caller has something to show.
 */
export function Reserve(props: {
  readonly children: ReactNode;
  readonly collapseInNarrowWindow?: boolean;
}): JSX.Element {
  return (
    <div
      className="vs-reserve"
      aria-hidden="true"
      inert
      data-collapse-narrow={props.collapseInNarrowWindow || undefined}
    >
      {props.children}
    </div>
  );
}

/**
 * The main area: the page gutter, and content centred at `width`. Without a `header` or `footer`
 * the page scrolls as one document, which suits a short page. With either, the page never
 * scrolls: the `header` (a title, a logo, the page's actions) and the `footer` (the page's primary
 * action) stay fully in view and the body between them scrolls on its own, with a faint hint at
 * its bottom edge while more lies below. A header or footer is for a page whose body can outgrow
 * the window. The header is also the page's header row for the window's toasts, which sit just
 * below it: put the title row and its actions there, not in the body.
 *
 * A page with a header opens on a trail band, the room of the breadcrumb that says where the reader
 * is, kept empty when the page has no `trail`, so that the title sits at the same place on every page
 * of the window and no page's title moves when the reader goes into one and back out. The header is
 * either the caller's own node or, with `title`, the page's own title row: the `<h1>` (which takes
 * the focus when a screen is opened, so a reader hears what changed) with the page's `actions`
 * beside it, and `meta` (its state and what it is about) under it.
 */
export function Page(props: {
  readonly width?: PageWidth;
  /** Keep the primary action beside a long form on wide screens. */
  readonly sideAction?: boolean;
  /** What stays in view above a body that scrolls. */
  readonly header?: ReactNode;
  /** The page's title as its `<h1>`, with `actions` and `meta`: the header, when it is not given whole. */
  readonly title?: string;
  /** The page's actions, in one group beside its title. */
  readonly actions?: ReactNode;
  /** What the page is about, in one row under its title: its state, what it is on. */
  readonly meta?: ReactNode;
  /** Where the reader is, over the title: each level but the last a way back to it. A page with none has no band for it. */
  readonly trail?: readonly Crumb[];
  /** What stays in view below a body that scrolls: the page's primary action. */
  readonly footer?: ReactNode;
  /**
   * The body does not scroll: it is the room between the header and the footer, and one child of it
   * (a `Conversation` with size `fill`, a log) takes what the others leave and scrolls by itself, so the
   * page has one scroll, not two. The page still scrolls when the window is too short for the body.
   */
  readonly fill?: boolean;
  /**
   * Whether what the page shows has settled, as `data-state` on the page. Required: a page that
   * reads nothing says `ready`, so no page is silent about it.
   */
  readonly state: PageState;
  readonly children: ReactNode;
}): JSX.Element {
  const width = props.width ?? 'content';
  const {title} = props;
  const arrival = useContext(PageArrival);
  // This callback changes only with the window's arrival, so it fires once as the page is drawn.
  const arrived = useCallback(
    (page: HTMLElement | null) => {
      if (page !== null) arrival(page);
    },
    [arrival],
  );
  const header =
    title === undefined ? (
      props.header
    ) : (
      <>
        <div className="vs-page-title-row">
          <h1 className="vs-page-title" tabIndex={-1} title={title}>
            {title}
          </h1>
          {props.actions === undefined ? null : (
            <div className="vs-page-actions">{props.actions}</div>
          )}
        </div>
        {props.meta === undefined ? null : <div className="vs-page-meta">{props.meta}</div>}
      </>
    );
  if (header === undefined && props.footer === undefined) {
    return (
      <ScrollArea
        as="main"
        kind="column"
        className="vs-page"
        data-state={props.state}
        ref={arrived}
      >
        <div className="vs-page-content" data-width={width}>
          {props.children}
        </div>
      </ScrollArea>
    );
  }
  return (
    <main
      className="vs-page"
      ref={arrived}
      data-fixed-header=""
      data-side-action={props.sideAction || undefined}
      data-state={props.state}
    >
      <div className="vs-page-content" data-width={width}>
        {header === undefined ? null : (
          <div className="vs-page-header">
            {props.trail === undefined || props.trail.length === 0 ? null : (
              <div className="vs-page-trail">
                <Breadcrumb label="Where you are" items={props.trail} />
              </div>
            )}
            {header}
          </div>
        )}
        <ScrollArea kind="column" className="vs-page-scroll" data-fill={props.fill || undefined}>
          <div className="vs-page-body">{props.children}</div>
        </ScrollArea>
        {props.footer === undefined ? null : <div className="vs-page-footer">{props.footer}</div>}
      </div>
    </main>
  );
}

/**
 * A screen with a panel docked at its right edge, full height. The screen keeps all its content
 * and narrows to make room; with no `side` it fills the whole width. `label` names the panel.
 */
export function Dock(props: {
  readonly label: string;
  readonly side: ReactNode | null;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-dock">
      <div className="vs-dock-main">{props.children}</div>
      {props.side === null ? null : (
        <section className="vs-dock-side" aria-label={props.label}>
          {props.side}
        </section>
      )}
    </div>
  );
}

/**
 * The free room of the `Row` or `Stack` it sits in, taken whole, so what follows it is pushed to
 * that container's end. It never shrinks below a `space-4` step, so when the container is too short
 * for what is on either side, they stay apart and the container scrolls instead of overlapping them.
 */
export function Spacer(): JSX.Element {
  return <div className="vs-spacer" aria-hidden="true" />;
}

/** Children in a column, `gap` apart. */
export function Stack(props: FlexProps): JSX.Element {
  return <Flex {...props} direction="column" gap={props.gap ?? 'space-4'} />;
}

/** Children in a line, `gap` apart; with `wrap` they flow onto more lines. */
export function Row(props: FlexProps): JSX.Element {
  return <Flex {...props} direction="row" align={props.align ?? 'center'} />;
}

/**
 * A main column and an aside beside it; the aside moves under the main column when narrow.
 * `asideFirst` puts the aside before the main column, in reading order and on screen (a list
 * beside the detail of its selected row); when narrow it then sits above it.
 */
export function Split(props: {
  readonly main: ReactNode;
  readonly aside: ReactNode;
  readonly gap?: SpaceToken;
  readonly asideFirst?: boolean;
  /**
   * The aside is at the start (the left) when the two sit side by side, yet the main column still
   * comes first in reading order and, when narrow, on screen: what the page is about before the list
   * it was picked from.
   */
  readonly asideAtStart?: boolean;
  /** The aside stays in view beside a long main column, and scrolls on its own when it is taller than a thread. */
  readonly asideSticky?: boolean;
}): JSX.Element {
  const main = <div className="vs-split-main">{props.main}</div>;
  const aside =
    props.asideSticky === true ? (
      <ScrollArea kind="column" className="vs-split-aside" data-sticky="">
        {props.aside}
      </ScrollArea>
    ) : (
      <div className="vs-split-aside">{props.aside}</div>
    );
  return (
    <div
      className="vs-split"
      data-aside-at-start={props.asideAtStart === true || undefined}
      style={gapStyle(props.gap ?? 'space-5')}
    >
      {props.asideFirst === true ? aside : main}
      {props.asideFirst === true ? main : aside}
    </div>
  );
}

/**
 * A page's persistent section navigation beside independently changing content. With `summary`
 * (a one-line account of where the reader is, such as "4 of 8 · Hardware") the navigation is a
 * list of steps: beside the content while the layout is wide, and under that one line, which the
 * reader presses to open the list, once the layout is narrower.
 */
export function SectionLayout(props: {
  readonly label: string;
  readonly sections: ReactNode;
  readonly summary?: string;
  readonly children: ReactNode;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const {summary} = props;
  return (
    <div className="vs-section-container">
      <div className="vs-section-layout" data-steps={summary === undefined ? undefined : ''}>
        <nav className="vs-section-nav" aria-label={props.label}>
          {summary === undefined ? (
            props.sections
          ) : (
            <>
              <button
                type="button"
                className="vs-section-toggle"
                aria-expanded={open}
                aria-controls={listId}
                onClick={() => setOpen(!open)}
              >
                <span className="vs-section-toggle-text">{summary}</span>
                <span className="vs-tree-chevron" data-open={open || undefined}>
                  <Icon name="chevron-right" />
                </span>
              </button>
              <div className="vs-section-steps" id={listId} data-open={open || undefined}>
                {props.sections}
              </div>
            </>
          )}
        </nav>
        <div className="vs-section-content">{props.children}</div>
      </div>
    </div>
  );
}

/**
 * A main column and a side column of a fixed width beside it. Which holds is decided by the width
 * of what holds the pair, not of the window: below about 1180 px the side goes under the main
 * column and its children share a row as far as they fit, so the page never scrolls sideways and
 * nothing has to be hidden. Both columns keep their children where they are as the data grows.
 */
export function MainSide(props: {
  /** Names the pair of columns for assistive technology. */
  readonly label: string;
  readonly main: ReactNode;
  readonly side: ReactNode;
}): JSX.Element {
  return (
    <section className="vs-mainside" aria-label={props.label}>
      <div className="vs-mainside-grid">
        <div className="vs-mainside-main">{props.main}</div>
        <div className="vs-mainside-side">{props.side}</div>
      </div>
    </section>
  );
}

/** As many equal columns as fit, none narrower than `min`. */
export function Grid(props: {
  readonly min: GridColumn;
  readonly gap?: SpaceToken;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-grid" data-min={props.min} style={gapStyle(props.gap ?? 'space-4')}>
      {props.children}
    </div>
  );
}
