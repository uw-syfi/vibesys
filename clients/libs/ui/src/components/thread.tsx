import {type JSX, type UIEvent, useRef, useState} from 'react';
import {SIZE} from '../tokens/index.js';
import {Button} from './button.js';
import {
  Message,
  type MessageTone,
  type MessageVariant,
  ToolCall,
  type ToolState,
} from './message.js';
import {type ReplyField, StructuredReply} from './structured-reply.js';
import {Text} from './text.js';

/** One entry of a thread: a heading that starts a section, a message, or a tool call. */
export type ThreadItem =
  | {
      readonly kind: 'heading';
      readonly id: string;
      readonly title: string;
      readonly detail?: string;
    }
  | {
      readonly kind: 'message';
      readonly id: string;
      readonly author: string;
      readonly variant: MessageVariant;
      readonly tone?: MessageTone;
      readonly text: string;
      readonly notice?: string;
      /** Open or closed until the reader decides; a reply and a note start open, the rest closed. */
      readonly startOpen?: boolean;
    }
  | {
      readonly kind: 'reply';
      readonly id: string;
      readonly author: string;
      readonly title: string;
      readonly fields: readonly ReplyField[];
      readonly raw: string;
      /** Open until the reader decides; a structured reply starts open. */
      readonly startOpen?: boolean;
    }
  | {
      readonly kind: 'tool';
      readonly id: string;
      readonly name: string;
      readonly state: ToolState;
      readonly summary?: string;
      readonly meta?: string;
      readonly input: string | null;
      readonly output: string | null;
      readonly startOpen?: boolean;
    };

/** The height a row is taken to have until it has been drawn and measured. */
const ESTIMATED_ROW = 72;
/** Rows drawn beyond the visible ones on each side, so a scroll does not show a gap. */
const OVERSCAN = 6;

function startsOpen(item: ThreadItem): boolean {
  if (item.kind === 'heading') return true;
  if (item.startOpen !== undefined) return item.startOpen;
  if (item.kind === 'reply') return true;
  return item.kind === 'message' && (item.variant === 'reply' || item.variant === 'note');
}

/** Where each item starts, and, last, the total height; `measured` holds the rows drawn so far. */
function offsetsOf(items: readonly ThreadItem[], measured: ReadonlyMap<string, number>): number[] {
  const offsets: number[] = [0];
  let at = 0;
  for (const item of items) {
    at += measured.get(item.id) ?? ESTIMATED_ROW;
    offsets.push(at);
  }
  return offsets;
}

/** The last index in `offsets` that is at or before `position`. */
function indexAt(offsets: readonly number[], position: number): number {
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if ((offsets[middle] ?? 0) <= position) low = middle;
    else high = middle - 1;
  }
  return low;
}

function atBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 2;
}

function Row(props: {
  readonly item: ThreadItem;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const {item, open, toggle} = props;
  if (item.kind === 'heading') {
    return (
      <div className="vs-thread-heading">
        <Text as="div" variant="body-strong">
          {item.title}
        </Text>
        {item.detail === undefined ? null : (
          <Text variant="small" tone="muted">
            {item.detail}
          </Text>
        )}
      </div>
    );
  }
  if (item.kind === 'message') {
    return (
      <Message
        author={item.author}
        variant={item.variant}
        {...(item.tone === undefined ? {} : {tone: item.tone})}
        text={item.text}
        {...(item.notice === undefined ? {} : {notice: item.notice})}
        open={open}
        toggle={toggle}
      />
    );
  }
  if (item.kind === 'reply') {
    return (
      <StructuredReply
        author={item.author}
        title={item.title}
        fields={item.fields}
        raw={item.raw}
        open={open}
        toggle={toggle}
      />
    );
  }
  return (
    <ToolCall
      name={item.name}
      state={item.state}
      {...(item.summary === undefined ? {} : {summary: item.summary})}
      {...(item.meta === undefined ? {} : {meta: item.meta})}
      input={item.input}
      output={item.output}
      open={open}
      toggle={toggle}
    />
  );
}

/**
 * A long conversation of messages and tool calls, in order, each opened and closed by the reader.
 * Only the rows near the visible part are drawn, between spacers as tall as the rest, so a
 * thread of any length costs a screenful of rows. A row's height is taken to be an estimate
 * until it has been drawn, then the height it was measured at. While scrolled to the end it follows
 * the newest item as items arrive and grow; scrolling up stops following and offers "Jump to
 * latest". Give it a `key` that changes with its content's identity, so a new thread starts at
 * its end with every block as it starts.
 */
export function Thread(props: {
  readonly label: string;
  readonly items: readonly ThreadItem[];
}): JSX.Element {
  const {items} = props;
  const following = useRef(true);
  const pane = useRef<HTMLUListElement | null>(null);
  const measured = useRef(new Map<string, number>());
  const [flipped, setFlipped] = useState<ReadonlySet<string>>(new Set());
  const [view, setView] = useState({top: 0, height: SIZE.thread});
  const [, setMeasures] = useState(0);
  const [behind, setBehind] = useState(false);

  const offsets = offsetsOf(items, measured.current);
  const total = offsets.at(-1) ?? 0;
  const top = following.current ? Math.max(0, total - view.height) : view.top;
  const first = Math.max(0, indexAt(offsets, top) - OVERSCAN);
  const last = Math.min(items.length, indexAt(offsets, top + view.height) + 1 + OVERSCAN);

  const follow = (element: HTMLUListElement | null): void => {
    pane.current = element;
    if (element === null) return;
    if (element.clientHeight > 0 && element.clientHeight !== view.height) {
      setView({top: view.top, height: element.clientHeight});
    }
    if (following.current) element.scrollTop = element.scrollHeight;
  };
  const scrolled = (event: UIEvent<HTMLUListElement>): void => {
    const element = event.currentTarget;
    following.current = atBottom(element);
    setView({top: element.scrollTop, height: element.clientHeight || view.height});
    setBehind(!following.current);
  };
  const jump = (): void => {
    following.current = true;
    setBehind(false);
    if (pane.current !== null) pane.current.scrollTop = pane.current.scrollHeight;
  };
  const measure = (id: string, row: HTMLElement | null): void => {
    if (row === null) return;
    const height = row.offsetHeight;
    if (height > 0 && measured.current.get(id) !== height) {
      measured.current.set(id, height);
      setMeasures(count => count + 1);
    }
  };
  const flip = (id: string): void => {
    const next = new Set(flipped);
    if (!next.delete(id)) next.add(id);
    setFlipped(next);
  };

  return (
    <div className="vs-thread">
      <ul
        ref={follow}
        className="vs-thread-pane"
        aria-label={props.label}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling box must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls text.
        tabIndex={0}
        onScroll={scrolled}
      >
        <li aria-hidden="true" className="vs-thread-gap" style={{height: offsets[first] ?? 0}} />
        {items.slice(first, last).map((item, offset) => {
          const open = startsOpen(item) !== flipped.has(item.id);
          return (
            <li
              key={item.id}
              ref={row => measure(item.id, row)}
              className="vs-thread-row"
              aria-posinset={first + offset + 1}
              aria-setsize={items.length}
            >
              <Row item={item} open={open} toggle={() => flip(item.id)} />
            </li>
          );
        })}
        <li
          aria-hidden="true"
          className="vs-thread-gap"
          style={{height: total - (offsets[last] ?? total)}}
        />
      </ul>
      {behind ? (
        <span className="vs-thread-jump">
          <Button
            size="sm"
            variant="primary"
            icon="chevron-down"
            text="Jump to latest"
            press={jump}
          />
        </span>
      ) : null}
    </div>
  );
}
