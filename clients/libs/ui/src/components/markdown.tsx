import {createContext, type JSX, type ReactNode, useContext} from 'react';
import ReactMarkdown, {type Components} from 'react-markdown';
import remarkGfm from 'remark-gfm';

const OpenLinkContext = createContext<((url: string) => void) | null>(null);

/**
 * Where the links of every `Markdown` below open. Give it the app's way of opening a URL outside
 * it; a link then calls `open` and never navigates the page. Without one, a link opens in a
 * new browsing context with no referrer.
 */
export function LinkOpener(props: {
  readonly open: (url: string) => void;
  readonly children: ReactNode;
}): JSX.Element {
  return <OpenLinkContext.Provider value={props.open}>{props.children}</OpenLinkContext.Provider>;
}

const OPENABLE = new Set(['http:', 'https:', 'mailto:']);

/** The URL when it leaves the app by a web or mail address; null for anything else. */
function openable(url: string): string | null {
  try {
    return OPENABLE.has(new URL(url).protocol) ? url : null;
  } catch {
    return null;
  }
}

function Anchor(props: {
  readonly href?: string | undefined;
  readonly children?: ReactNode;
}): JSX.Element {
  const open = useContext(OpenLinkContext);
  const href = props.href === undefined ? null : openable(props.href);
  // A link to nowhere the app may send the reader (a relative path, a script) is just its text.
  if (href === null) return <span>{props.children}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      onClick={
        open === null
          ? undefined
          : event => {
              event.preventDefault();
              open(href);
            }
      }
    >
      {props.children}
    </a>
  );
}

/** An image is not fetched: its description stands in, so no remote request is made. */
function Image(props: {readonly alt?: string | undefined}): JSX.Element {
  const alt = props.alt ?? '';
  return <span className="vs-markdown-image">{alt === '' ? 'image' : alt}</span>;
}

// A heading is a bold line, not a heading element: the text sits inside a card or a turn, so a
// real h1 to h6 would break the page's heading order. `data-level` is the level the author wrote
// (from the tag the parser named), which sets the size.
function Heading(props: {
  readonly node?: {readonly tagName: string} | undefined;
  readonly children?: ReactNode;
}): JSX.Element {
  return (
    <p className="vs-markdown-heading" data-level={props.node?.tagName.slice(1)}>
      {props.children}
    </p>
  );
}

function CodeBlock(props: {readonly children?: ReactNode}): JSX.Element {
  return (
    <pre
      className="vs-markdown-pre"
      // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling box must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls text.
      tabIndex={0}
    >
      {props.children}
    </pre>
  );
}

function ScrollingTable(props: {readonly children?: ReactNode}): JSX.Element {
  return (
    <div
      className="vs-markdown-scroll"
      // biome-ignore lint/a11y/noNoninteractiveTabindex: as the code block: a wide table scrolls inside this box, which a keyboard must be able to reach.
      tabIndex={0}
    >
      <table>{props.children}</table>
    </div>
  );
}

const COMPONENTS: Components = {
  a: Anchor,
  img: Image,
  h1: Heading,
  h2: Heading,
  h3: Heading,
  h4: Heading,
  h5: Heading,
  h6: Heading,
  pre: CodeBlock,
  table: ScrollingTable,
};

const PLUGINS = [remarkGfm];

/**
 * Text an author wrote as Markdown (lists, tables, strikethrough, links, code and
 * emphasis, with GitHub's extensions), laid out compactly in the body type. Raw HTML in the text
 * is shown as text, never as elements; images are not loaded (their description shows); a link
 * opens outside the app (see `LinkOpener`) and only a web or mail address is a link. Long lines
 * wrap, and a code block or a table scrolls inside itself.
 */
export function Markdown(props: {readonly text: string}): JSX.Element {
  return (
    <div className="vs-markdown">
      <ReactMarkdown
        remarkPlugins={PLUGINS}
        components={COMPONENTS}
        urlTransform={url => openable(url) ?? ''}
      >
        {props.text.trim()}
      </ReactMarkdown>
    </div>
  );
}
