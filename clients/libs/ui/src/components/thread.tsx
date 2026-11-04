import {type JSX, type ReactNode, useState} from 'react';
import {
  Message,
  type MessageTone,
  type MessageVariant,
  ToolCall,
  type ToolState,
} from './message.js';
import {NewBelow} from './new-below.js';
import {type ReplyField, StructuredReply} from './structured-reply.js';
import {Text} from './text.js';
import {useWindowed} from './windowing.js';

/** One entry of a thread: a heading that starts a section, a message, a tool call, or a block of the caller's own. */
export type ThreadItem =
  | {
      readonly kind: 'node';
      readonly id: string;
      /** What the caller draws in this row (a card with its own controls). */
      readonly node: ReactNode;
    }
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

function startsOpen(item: ThreadItem): boolean {
  if (item.kind === 'heading' || item.kind === 'node') return true;
  if (item.startOpen !== undefined) return item.startOpen;
  if (item.kind === 'reply') return true;
  return item.kind === 'message' && (item.variant === 'reply' || item.variant === 'note');
}

function Row(props: {
  readonly item: ThreadItem;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const {item, open, toggle} = props;
  if (item.kind === 'node') return <>{item.node}</>;
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
 * the newest item as items arrive and grow; scrolling up stops following and shows a "New below"
 * pill that jumps to the end. `size` `fill` takes the height of its container. Give it a `key` that changes with its content's identity, so a new thread starts at
 * its end with every block as it starts.
 */
export function Thread(props: {
  readonly label: string;
  readonly items: readonly ThreadItem[];
  readonly size?: 'default' | 'fill';
  /** No frame and no dividers: rows sit on the surface around the thread, as in a chat. */
  readonly plain?: boolean;
  /**
   * The id of the row the thread opens at, whole at the top edge (a chat opens at its latest
   * question), until the reader scrolls or `toEnd` changes; without it the thread opens at its end.
   */
  readonly opensAt?: string;
  /** A number that, each time it changes, scrolls the thread to its end (a new question). */
  readonly toEnd?: number;
}): JSX.Element {
  const {items} = props;
  const [flipped, setFlipped] = useState<ReadonlySet<string>>(new Set());
  const opensAt =
    props.opensAt === undefined ? -1 : items.findIndex(item => item.id === props.opensAt);
  const win = useWindowed({
    count: items.length,
    keyOf: index => items[index]?.id ?? String(index),
    estimate: ESTIMATED_ROW,
    follow: true,
    ...(props.toEnd === undefined ? {} : {toEnd: props.toEnd}),
    ...(opensAt < 0 ? {} : {startAt: opensAt}),
  });
  const flip = (id: string): void => {
    const next = new Set(flipped);
    if (!next.delete(id)) next.add(id);
    setFlipped(next);
  };

  return (
    <div
      className="vs-thread"
      data-size={props.size ?? 'default'}
      data-plain={props.plain === true || undefined}
    >
      <ul
        ref={win.scroller}
        className="vs-thread-pane"
        aria-label={props.label}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling box must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls text.
        tabIndex={0}
        onScroll={win.scrolled}
        onWheel={win.touched}
        onTouchMove={win.touched}
        onPointerDown={win.touched}
        onKeyDown={win.touched}
      >
        <li aria-hidden="true" className="vs-thread-gap" style={{height: win.before}} />
        {items.slice(win.first, win.last).map((item, offset) => {
          const open = startsOpen(item) !== flipped.has(item.id);
          const slot = win.slotOf(win.first + offset);
          return (
            <li
              key={item.id}
              ref={slot.measure}
              className="vs-thread-row"
              aria-posinset={slot.index + 1}
              aria-setsize={items.length}
            >
              <Row item={item} open={open} toggle={() => flip(item.id)} />
            </li>
          );
        })}
        <li aria-hidden="true" className="vs-thread-gap" style={{height: win.after}} />
      </ul>
      {win.behind ? (
        <span className="vs-thread-jump">
          <NewBelow text="New below" press={win.jump} />
        </span>
      ) : null}
    </div>
  );
}
