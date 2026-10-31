import {Children, type JSX, type ReactNode, useMemo} from 'react';
import {Collapsible} from './collapsible.js';
import {Icon, type IconName} from './icon.js';
import {Markdown} from './markdown.js';
import {markdownToPlain} from './markdown-plain.js';
import type {ToolState} from './message.js';
import {NewBelow} from './new-below.js';
import {InSlot, useSlot, useWindowed} from './windowing.js';

/** The height a block is taken to have until it has been drawn and measured. */
const ESTIMATED_BLOCK = 56;

/**
 * A turn read as a narrative: `Thought`, `Said` and `ToolGroup` blocks in time order, the words
 * first and the tool calls grouped between them. It scrolls inside itself (`size` `fill` takes the
 * height of its container) and draws only the blocks near the visible part, between spacers as
 * tall as the rest, so a turn of any length costs a screenful. Give each block a stable `key`.
 * While following the end (`start` `end`), scrolling up stops following and shows a "New below"
 * pill that jumps back to the end, as `LogPane` does.
 */
export function Narrative(props: {
  readonly label: string;
  readonly size?: 'default' | 'fill';
  /** `end` opens scrolled to the last block and stays there until scrolled up (a live turn). */
  readonly start?: 'top' | 'end';
  /** A number that, each time it changes, scrolls to the last block. */
  readonly toEnd?: number;
  readonly children: ReactNode;
}): JSX.Element {
  const blocks = Children.toArray(props.children);
  const win = useWindowed({
    count: blocks.length,
    keyOf: index => String((blocks[index] as {readonly key?: string} | undefined)?.key ?? index),
    estimate: ESTIMATED_BLOCK,
    follow: props.start === 'end',
    ...(props.toEnd === undefined ? {} : {toEnd: props.toEnd}),
  });
  return (
    <div className="vs-narrative-wrap" data-size={props.size ?? 'default'}>
      <section
        ref={win.scroller}
        className="vs-narrative"
        data-size={props.size ?? 'default'}
        aria-label={props.label}
        onScroll={win.scrolled}
      >
        <div aria-hidden="true" style={{height: win.before}} />
        {blocks.slice(win.first, win.last).map((block, offset) => (
          <InSlot
            key={(block as {readonly key?: string}).key ?? win.first + offset}
            slot={win.slotOf(win.first + offset)}
          >
            {block}
          </InSlot>
        ))}
        <div aria-hidden="true" style={{height: win.after}} />
      </section>
      {win.behind ? (
        <span className="vs-narrative-jump">
          <NewBelow text="New below" press={win.jump} />
        </span>
      ) : null}
    </div>
  );
}

/** How much of a thought the closed line reads: a few hundred characters is far more than a line shows. */
const PREVIEW_CHARS = 400;

/**
 * The closed line's preview: the start of the text as plain words, its blocks (a bold title, then a
 * sentence) told apart by a dot. Only the start is converted, so a long thought costs no more.
 */
function previewOf(text: string): string {
  const cut = text.slice(0, PREVIEW_CHARS);
  // Do not end on the first half of a character the cut split in two.
  const whole = /[\ud800-\udbff]$/.test(cut) ? cut.slice(0, -1) : cut;
  return whole
    .split(/\n[ \t]*\n/)
    .map(markdownToPlain)
    .filter(part => part !== '')
    .join(' · ');
}

/**
 * The thinking, as Markdown under a "Thought" line with a brain icon: muted and italic, so it
 * stands apart from what was said. Closed, the line carries a one-line preview of the text. While
 * it is still being written (`live`) the line reads "Thinking" and its icon pulses. The caller
 * owns whether it is open, as for `ToolGroup`.
 */
export function Thought(props: {
  readonly text: string;
  readonly live?: boolean;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const preview = useMemo(() => previewOf(props.text), [props.text]);
  return (
    <div
      ref={useSlot()?.measure}
      className="vs-thought"
      data-live={props.live === true || undefined}
    >
      <Collapsible
        label={props.live === true ? 'Thinking' : 'Thought'}
        leading={
          <span className="vs-thought-icon">
            <Icon name="brain" />
          </span>
        }
        {...(props.open || preview === '' ? {} : {summary: preview})}
        open={props.open}
        toggle={props.toggle}
      >
        <Markdown text={props.text} />
      </Collapsible>
    </div>
  );
}

/** What was said, as Markdown in body text. */
export function Said(props: {readonly text: string}): JSX.Element {
  return (
    <div ref={useSlot()?.measure} className="vs-said">
      <Markdown text={props.text} />
    </div>
  );
}

/** One tool call inside a `ToolGroup`: its name, its subject and its outcome in a few words. */
export interface ToolLine {
  readonly id: string;
  readonly name: string;
  readonly line: string;
  readonly outcome?: string;
  /** When in the turn the call was made, as the row writes it (`+1m 17s`). */
  readonly time?: string;
  /** What the time means, for a reader who hovers it. */
  readonly timeTitle?: string;
  readonly state: ToolState;
  readonly selected?: boolean;
  readonly press: () => void;
}

/** The icon and the word that say where a call stands. */
const STATE: Readonly<Record<ToolState, {readonly icon: IconName; readonly word: string}>> = {
  pending: {icon: 'wrench', word: 'In progress'},
  ok: {icon: 'check', word: 'Done'},
  failed: {icon: 'alert', word: 'Failed'},
};

function toolWords(tool: ToolLine): string {
  return `${tool.name} ${tool.line}${tool.outcome === undefined ? '' : ` (${tool.outcome})`}`;
}

function ToolRow(props: {readonly tool: ToolLine}): JSX.Element {
  const {tool} = props;
  const state = STATE[tool.state];
  return (
    <button
      type="button"
      className="vs-toolgroup-row"
      title={toolWords(tool)}
      data-state={tool.state}
      aria-current={tool.selected === true ? 'true' : undefined}
      onClick={tool.press}
    >
      <span className="vs-toolgroup-status">
        <Icon name={state.icon} label={state.word} />
      </span>
      <span className="vs-toolgroup-name">{tool.name}</span>
      <span className="vs-toolgroup-line">{tool.line}</span>
      {tool.outcome === undefined ? null : (
        <span className="vs-toolgroup-outcome">{tool.outcome}</span>
      )}
      {tool.time === undefined ? null : (
        <span
          className="vs-toolgroup-time"
          {...(tool.timeTitle === undefined ? {} : {title: tool.timeTitle})}
        >
          {tool.time}
        </span>
      )}
    </button>
  );
}

/**
 * The tool calls made between two paragraphs: a head ("2 tools") that opens in place to one row
 * per call, each with a status icon, a name chip, its subject, its outcome and when in the turn it
 * ran. Closed, the head lists the calls in one dim line and a failed call stays visible as its
 * own red row. Pressing a row opens that call. The caller owns whether the group is open.
 */
export function ToolGroup(props: {
  readonly tools: readonly ToolLine[];
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  const slot = useSlot();
  const failed = props.tools.filter(tool => tool.state === 'failed');
  const count = `${props.tools.length} ${props.tools.length === 1 ? 'tool' : 'tools'}`;
  return (
    <div ref={slot?.measure} className="vs-toolgroup" data-open={props.open || undefined}>
      <button
        type="button"
        className="vs-toolgroup-head"
        title={props.tools.map(toolWords).join('\n')}
        aria-expanded={props.open}
        onClick={() => props.toggle(!props.open)}
      >
        <Icon name={props.open ? 'chevron-down' : 'chevron-right'} />
        <span className="vs-toolgroup-count">{count}</span>
        {props.open ? null : (
          <span className="vs-toolgroup-line">{props.tools.map(toolWords).join(', ')}</span>
        )}
      </button>
      {(props.open ? props.tools : failed).map(tool => (
        <ToolRow key={tool.id} tool={tool} />
      ))}
    </div>
  );
}
