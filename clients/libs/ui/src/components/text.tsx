import type {JSX, ReactNode} from 'react';
import type {TextStyle} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';

/** The color text is drawn in: the body's, secondary, or a status. */
export type TextTone = 'default' | 'muted' | 'ok' | 'warn' | 'err';
/** The element a `Text` renders, for the document outline; its look comes from `style` alone. */
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

/** Text in one of the type scale's styles. A `section` label is `muted` unless `tone` says otherwise. */
export function Text(props: {
  readonly style?: TextStyle;
  readonly tone?: TextTone;
  readonly as?: TextElement;
  /** For a region or control to be named by this text (`aria-labelledby`). */
  readonly id?: string;
  readonly children: ReactNode;
}): JSX.Element {
  const {style = 'body', as: Element = 'span'} = props;
  const tone = props.tone ?? (style === 'section' ? 'muted' : 'default');
  return (
    <Element className="vs-text" id={props.id} data-tone={tone} style={typeStyle(style)}>
      {props.children}
    </Element>
  );
}

function columns(art: string): number {
  return Math.max(1, ...art.split('\n').map(line => [...line].length));
}

/**
 * Preformatted ASCII art in the `display` style, scaled down to fit its container's width. The art
 * is the caller's; `label` is its accessible name.
 */
export function Wordmark(props: {readonly art: string; readonly label: string}): JSX.Element {
  const style: StyleWithVars = {'--wordmark-columns': columns(props.art)};
  return (
    <div className="vs-wordmark">
      <pre className="vs-wordmark-art" role="img" aria-label={props.label} style={style}>
        {props.art}
      </pre>
    </div>
  );
}
