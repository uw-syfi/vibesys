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
  /**
   * A fixed width, whatever the content: a figure's block (`figure`), so what shares its row never moves; or
   * a chart (`chart`), which takes the rest of its row and drops to a row of its own, full width, when the
   * row leaves it less than a readable width.
   */
  readonly width?: 'figure' | 'chart';
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

/**
 * The room `children` would take, kept empty: it is invisible, unreachable and unannounced. A line
 * that appears and disappears (a state's name before there is a state) is drawn here while absent,
 * so what shares its column never moves when it comes.
 */
export function Reserve(props: {readonly children: ReactNode}): JSX.Element {
  return (
    <div className="vs-reserve" aria-hidden="true" inert>
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
 * the window.
 */
export function Page(props: {
  readonly width?: PageWidth;
  /** Keep the primary action beside a long form on wide screens. */
  readonly sideAction?: boolean;
  /** What stays in view above a body that scrolls. */
  readonly header?: ReactNode;
  /** What stays in view below a body that scrolls: the page's primary action. */
  readonly footer?: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  const width = props.width ?? 'content';
  if (props.header === undefined && props.footer === undefined) {
    return (
      <main className="vs-page">
        <div className="vs-page-content" data-width={width}>
          {props.children}
        </div>
      </main>
    );
  }
  return (
    <main className="vs-page" data-fixed-header="" data-side-action={props.sideAction || undefined}>
      <div className="vs-page-content" data-width={width}>
        {props.header === undefined ? null : <div className="vs-page-header">{props.header}</div>}
        <div className="vs-page-scroll">
          <div className="vs-page-body">{props.children}</div>
        </div>
        {props.footer === undefined ? null : <div className="vs-page-footer">{props.footer}</div>}
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
 * beside the detail of its selected row); when narrow it then sits above it.
 */
export function Split(props: {
  readonly main: ReactNode;
  readonly aside: ReactNode;
  readonly gap?: SpaceToken;
  readonly asideFirst?: boolean;
}): JSX.Element {
  const main = <div className="vs-split-main">{props.main}</div>;
  const aside = <div className="vs-split-aside">{props.aside}</div>;
  return (
    <div className="vs-split" style={gapStyle(props.gap ?? 'space-5')}>
      {props.asideFirst === true ? aside : main}
      {props.asideFirst === true ? main : aside}
    </div>
  );
}

/** A page's persistent section navigation beside independently changing content. */
export function SectionLayout(props: {
  readonly label: string;
  readonly sections: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-section-container">
      <div className="vs-section-layout">
        <nav className="vs-section-nav" aria-label={props.label}>
          {props.sections}
        </nav>
        <div className="vs-section-content">{props.children}</div>
      </div>
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
