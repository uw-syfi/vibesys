import {type JSX, type UIEvent, useState} from 'react';
import {SIZE} from '../tokens/index.js';
import {Button} from './button.js';
import {ChatMessage} from './chat-message.js';
import {useEntering} from './entering.js';
import {Icon, type IconName} from './icon.js';
import {ToolCall, type ToolState} from './message.js';
import {Said, Thought} from './narrative.js';
import {NewBelowSlot} from './new-below.js';
import {ScrollArea} from './scroll-area.js';
import {Chip, type StatusTone} from './status.js';
import {Text} from './text.js';
import {useWindowed} from './windowing.js';

/** The height a row is taken to have until it has been drawn and measured. */
const ESTIMATED_ROW = 72;
/** A scroll this close to the top asks for earlier rows. */
const NEAR_TOP = SIZE.row * 4;

/** Who wrote a message: a name, which opens its author when it can. */
export interface ConversationAuthor {
  readonly label: string;
  readonly press?: () => void;
}

/**
 * One row of a `Conversation`. Rows share one left edge and one measure; only a `bubble`, the
 * message that started the turn, sits at the end of the line.
 */
export type ConversationItem =
  /** A thin rule across the conversation with a label, where a new section begins. */
  | {readonly kind: 'rule'; readonly id: string; readonly label: string}
  /** What was said to the one whose conversation this is, with who said it and when. */
  | {
      readonly kind: 'bubble';
      readonly id: string;
      readonly author: ConversationAuthor;
      readonly at: string;
      readonly text: string;
    }
  /** What the one whose conversation this is wrote, as Markdown. */
  | {readonly kind: 'said'; readonly id: string; readonly text: string}
  /** Its thinking, folded under a line such as "Thought for 14s". */
  | {
      readonly kind: 'thought';
      readonly id: string;
      readonly label: string;
      readonly text: string;
      /** Said in place of text that has not been read yet. */
      readonly notice?: string;
    }
  /** One tool call as a line: its name, a short argument, its state and how long it took. */
  | {
      readonly kind: 'tool';
      readonly id: string;
      readonly name: string;
      readonly state: ToolState;
      readonly summary: string;
      readonly duration?: string;
      readonly input: string | null;
      readonly output: string | null;
      /** Said in place of a result that has not been read yet. */
      readonly notice?: string;
    }
  /** A row that leads to another conversation: something was spawned or came back. */
  | {
      readonly kind: 'link';
      readonly id: string;
      readonly icon: IconName;
      readonly title: string;
      readonly chip?: {readonly label: string; readonly tone: StatusTone};
      readonly trail: string;
      /** A few lines under the title, such as the reason behind its chip; the pointer shows all of it. */
      readonly note?: string;
      readonly press: () => void;
    };

/** Whether the earlier rows are being read, and how to ask for them. */
export interface ConversationEarlier {
  readonly text: string;
  readonly loading: boolean;
  readonly load: () => void;
}

function LinkRow(props: {readonly item: Extract<ConversationItem, {kind: 'link'}>}): JSX.Element {
  const {item} = props;
  return (
    <div className="vs-conversation-link">
      <div className="vs-conversation-link-head">
        <Icon name={item.icon} />
        <Button variant="quiet" size="sm" text={item.title} press={item.press} />
        {item.chip === undefined ? null : <Chip label={item.chip.label} tone={item.chip.tone} />}
        <span className="vs-conversation-trail">{item.trail}</span>
      </div>
      {item.note === undefined ? null : (
        <div className="vs-conversation-link-note">
          <Text variant="small" tone="muted" title={item.note}>
            {item.note}
          </Text>
        </div>
      )}
    </div>
  );
}

function Bubble(props: {readonly item: Extract<ConversationItem, {kind: 'bubble'}>}): JSX.Element {
  const {item} = props;
  const {author} = item;
  return (
    <div className="vs-conversation-bubble">
      <span className="vs-conversation-from">
        {author.press === undefined ? (
          <Text variant="small" tone="muted">
            {author.label}
          </Text>
        ) : (
          <Button variant="quiet" size="sm" text={author.label} press={author.press} />
        )}
        <span className="vs-conversation-trail">{item.at}</span>
      </span>
      <ChatMessage side="end" text={item.text} />
    </div>
  );
}

function Row(props: {
  readonly item: ConversationItem;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const {item, open, toggle} = props;
  switch (item.kind) {
    case 'rule':
      return (
        <div className="vs-conversation-rule">
          <Text variant="small" tone="muted">
            <span className="vs-conversation-trail">{item.label}</span>
          </Text>
        </div>
      );
    case 'bubble':
      return <Bubble item={item} />;
    case 'said':
      return <Said text={item.text} />;
    case 'thought':
      return (
        <Thought
          text={item.text}
          label={item.label}
          {...(item.notice === undefined ? {} : {notice: item.notice})}
          open={open}
          toggle={toggle}
        />
      );
    case 'tool':
      return (
        <ToolCall
          name={item.name}
          state={item.state}
          summary={item.summary}
          {...(item.duration === undefined ? {} : {meta: item.duration})}
          {...(item.notice === undefined ? {} : {notice: item.notice})}
          input={item.input}
          output={item.output}
          open={open}
          toggle={toggle}
        />
      );
    case 'link':
      return <LinkRow item={item} />;
    default:
      item satisfies never;
      return <span />;
  }
}

/**
 * A conversation read top to bottom: rules between its sections, the message that began each, what
 * was written, the thinking and the tool calls folded to a line each, and rows that lead to other
 * conversations. It scrolls inside itself and draws only the rows near the visible part. It opens at
 * its end and stays there as rows arrive (`New below` after a scroll up); rows that arrive draw in
 * with a short motion. With `earlier`, a button above the first row and a scroll near the top both
 * ask for the rows before it, and the rows the reader is looking at stay where they are when those
 * arrive. A thinking block or a tool call opens and closes by the reader; `opened` hears each change
 * so a body that is read on demand can be asked for. Long lines of code scroll inside their block.
 */
export function Conversation(props: {
  readonly label: string;
  readonly items: readonly ConversationItem[];
  readonly earlier?: ConversationEarlier | null;
  readonly opened?: (id: string, open: boolean) => void;
  /** `fill` takes the height of its container; the default is most of the window's. */
  readonly size?: 'default' | 'fill';
}): JSX.Element {
  const {items, earlier} = props;
  const [flipped, setFlipped] = useState<ReadonlySet<string>>(new Set());
  const entering = useEntering(
    items.map(item => item.id),
    'at-end',
  );
  const win = useWindowed({
    count: items.length,
    keyOf: index => items[index]?.id ?? String(index),
    estimate: ESTIMATED_ROW,
    follow: true,
    anchor: true,
  });
  const flip = (id: string, open: boolean): void => {
    const next = new Set(flipped);
    if (!next.delete(id)) next.add(id);
    setFlipped(next);
    props.opened?.(id, open);
  };
  const scrolled = (event: UIEvent<HTMLElement>): void => {
    win.scrolled(event);
    if (earlier != null && !earlier.loading && event.currentTarget.scrollTop < NEAR_TOP) {
      earlier.load();
    }
  };
  return (
    <div className="vs-conversation" data-size={props.size ?? 'default'}>
      <ScrollArea
        as="ul"
        kind="feed"
        ref={win.scroller}
        className="vs-conversation-pane"
        aria-label={props.label}
        tabIndex={0}
        onScroll={scrolled}
        onWheel={win.touched}
        onTouchMove={win.touched}
        onPointerDown={win.touched}
        onKeyDown={win.touched}
      >
        {earlier == null ? null : (
          <li className="vs-conversation-earlier">
            <Button
              variant="secondary"
              size="sm"
              text={earlier.text}
              press={earlier.load}
              {...(earlier.loading ? {disabledReason: earlier.text, quietReason: true} : {})}
            />
          </li>
        )}
        <li aria-hidden="true" className="vs-conversation-gap" style={{height: win.before}} />
        {items.slice(win.first, win.last).map((item, offset) => {
          const slot = win.slotOf(win.first + offset);
          const open = flipped.has(item.id);
          return (
            <li
              key={item.id}
              ref={slot.measure}
              className="vs-conversation-row"
              data-kind={item.kind}
              data-entering={entering.has(item.id) || undefined}
              aria-posinset={slot.index + 1}
              aria-setsize={items.length}
            >
              <Row item={item} open={open} toggle={next => flip(item.id, next)} />
            </li>
          );
        })}
        <li aria-hidden="true" className="vs-conversation-gap" style={{height: win.after}} />
      </ScrollArea>
      {win.behind ? <NewBelowSlot text="New below" press={win.jump} /> : null}
    </div>
  );
}
