import type {JSX, ReactNode} from 'react';
import type {TextStyle} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';

/** The color text is drawn in: the body's, secondary, or a status. */
export type TextTone = 'default' | 'muted' | 'ok' | 'warn' | 'err';
/** The element a `Text` renders, for the document outline; its look comes from `variant` alone. */
export type TextElement = 'span' | 'p' | 'div' | 'h1' | 'h2' | 'h3' | 'h4';

function typeStyle(style: TextStyle): StyleWithVars {
  const token = `--text-${style}`;
  return {
    fontFamily: `var(${token}-family)`,
    fontSize: `var(${token}-size)`,
    lineHeight: `var(${token}-line)`,
    fontWeight: `var(${token}-weight)`,
    letterSpacing: `var(${token}-tracking)`,
    '--text-transform': `var(${token}-transform)`,
  };
}

/**
 * Text in one of the type scale's styles (`variant`). A `section` label is `muted` unless `tone`
 * says otherwise. The prop is not named `style`, which strict packages may not pass to anything.
 */
export function Text(props: {
  readonly variant?: TextStyle;
  readonly tone?: TextTone;
  readonly as?: TextElement;
  /** For a region or control to be named by this text (`aria-labelledby`). */
  readonly id?: string;
  /** What the pointer shows over the text: the same thing in full, or what the text abbreviates. */
  readonly title?: string;
  readonly children: ReactNode;
}): JSX.Element {
  const {variant = 'body', as: Element = 'span'} = props;
  const tone = props.tone ?? (variant === 'section' ? 'muted' : 'default');
  return (
    <Element
      className="vs-text"
      id={props.id}
      title={props.title}
      data-tone={tone}
      data-mono-id={variant === 'code' || undefined}
      style={typeStyle(variant)}
    >
      {props.children}
    </Element>
  );
}

function columns(art: string): number {
  return Math.max(1, ...art.split('\n').map(line => [...line].length));
}

/** How a wordmark is colored: solid `accent`, or a gradient from `text` through `accent` to `muted`. */
export type WordmarkTone = 'accent' | 'gradient';

/** How big a wordmark is: `hero` fills its container, `compact` is a fixed 40px tall. */
export type WordmarkSize = 'hero' | 'compact';

/**
 * Preformatted ASCII art in the `display` style, sized to its container's width (the art takes
 * about three fifths of it, up to the style's size), set solid so its block glyphs join into
 * letters. `compact` instead draws it 40px tall whatever the width or the fonts, so nothing moves
 * when it loads. The art is the caller's; `label` is its accessible name, and with no `label` the
 * art is decoration, hidden from assistive technology.
 */
export function Wordmark(props: {
  readonly art: string;
  readonly label?: string;
  readonly align?: 'start' | 'center';
  readonly tone?: WordmarkTone;
  readonly size?: WordmarkSize;
}): JSX.Element {
  const style: StyleWithVars = {
    '--wordmark-columns': columns(props.art),
    '--wordmark-lines': props.art.split('\n').length,
  };
  return (
    <div
      className="vs-wordmark"
      data-align={props.align ?? 'start'}
      data-tone={props.tone ?? 'accent'}
      data-size={props.size ?? 'hero'}
    >
      <pre
        className="vs-wordmark-art"
        {...(props.label === undefined
          ? {'aria-hidden': true}
          : {role: 'img', 'aria-label': props.label})}
        style={style}
      >
        {props.art}
      </pre>
    </div>
  );
}
