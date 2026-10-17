import type {CSSProperties, JSX, ReactNode} from 'react';
import type {SpaceToken} from '../tokens/index.js';

/** The page content's max width: `content` (1060), `wide` (1100) or `narrow` (900). */
export type PageWidth = 'content' | 'wide' | 'narrow';
export type Align = 'start' | 'center' | 'end' | 'stretch' | 'baseline';
export type Justify = 'start' | 'center' | 'end' | 'between';
/** The narrowest column a `Grid` wraps at: a form field, a tile, or a theme swatch. */
export type GridColumn = 'field' | 'tile' | 'swatch';

interface FlexProps {
  readonly gap?: SpaceToken;
  readonly align?: Align;
  readonly justify?: Justify;
  readonly wrap?: boolean;
  /** Take the free space of the `Row` or `Stack` this sits in. */
  readonly grow?: boolean;
  /** A fixed width, whatever the content: a figure's block (`figure`), so what shares its row never moves. */
  readonly width?: 'figure';
  readonly children: ReactNode;
}

function gapStyle(gap: SpaceToken): CSSProperties {
  return {gap: `var(--${gap})`};
}

function Flex(props: FlexProps & {readonly direction: 'row' | 'column'}): JSX.Element {
  const {direction, gap = 'space-3', align, justify, wrap = false, grow = false} = props;
  return (
    <div
      className="vs-flex"
      data-direction={direction}
      data-align={align}
      data-justify={justify}
      data-wrap={wrap || undefined}
      data-grow={grow || undefined}
      data-width={props.width}
      style={gapStyle(gap)}
    >
      {props.children}
    </div>
  );
}

/** The scrolling main area: the page gutter, and content centred at `width`. */
export function Page(props: {
  readonly width?: PageWidth;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <main className="vs-page">
      <div className="vs-page-content" data-width={props.width ?? 'content'}>
        {props.children}
      </div>
    </main>
  );
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
 * beside the detail of its selected row); when narrow it then sits above it. `stickyAside` keeps
 * the aside in view while a long main column scrolls past (the detail of a row picked far down a
 * list); an aside taller than the window scrolls within itself.
 */
export function Split(props: {
  readonly main: ReactNode;
  readonly aside: ReactNode;
  readonly gap?: SpaceToken;
  readonly asideFirst?: boolean;
  readonly stickyAside?: boolean;
}): JSX.Element {
  const main = <div className="vs-split-main">{props.main}</div>;
  const aside = <div className="vs-split-aside">{props.aside}</div>;
  return (
    <div
      className="vs-split"
      {...(props.stickyAside === true ? {'data-sticky-aside': ''} : {})}
      style={gapStyle(props.gap ?? 'space-5')}
    >
      {props.asideFirst === true ? aside : main}
      {props.asideFirst === true ? main : aside}
    </div>
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
