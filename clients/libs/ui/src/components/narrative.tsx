import {Children, type JSX, type ReactNode} from 'react';
import {Icon} from './icon.js';
import {Markdown} from './markdown.js';
import type {ToolState} from './message.js';
import {NewBelow} from './new-below.js';
import {InSlot, useSlot, useWindowed} from './windowing.js';

/** The height a block is taken to have until it has been drawn and measured. */
const ESTIMATED_BLOCK = 56;

/**
 * A turn read as a narrative: `Thought`, `Said` and `ToolGroup` blocks in time order, the words
 * first and the tool calls folded between them. It scrolls inside itself (`size` `fill` takes the
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

/** The thinking, as Markdown: muted and italic, so it stands apart from what was said. */
export function Thought(props: {readonly text: string}): JSX.Element {
  return (
    <div ref={useSlot()?.measure} className="vs-thought">
      <Markdown text={props.text} />
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
  readonly state: ToolState;
  readonly selected?: boolean;
  readonly press: () => void;
}

function toolWords(tool: ToolLine): string {
  return `${tool.name} ${tool.line}${tool.outcome === undefined ? '' : ` (${tool.outcome})`}`;
}

function ToolRow(props: {readonly tool: ToolLine}): JSX.Element {
  const {tool} = props;
  return (
    <button
      type="button"
      className="vs-toolgroup-row"
      title={toolWords(tool)}
      data-state={tool.state}
      aria-current={tool.selected === true ? 'true' : undefined}
      onClick={tool.press}
    >
      <span className="vs-toolgroup-name">{tool.name}</span>
      <span className="vs-toolgroup-line">{tool.line}</span>
      {tool.outcome === undefined ? null : (
        <span className="vs-toolgroup-outcome">{tool.outcome}</span>
      )}
    </button>
  );
}

/**
 * The tool calls made between two paragraphs, as one dim line ("2 tools · read …") that opens in
 * place to one row per call. A failed call stays visible as its own red row while the group is
 * closed. Pressing a row opens that call.
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
        <span className="vs-toolgroup-line">{props.tools.map(toolWords).join(', ')}</span>
      </button>
      {(props.open ? props.tools : failed).map(tool => (
        <ToolRow key={tool.id} tool={tool} />
      ))}
    </div>
  );
}
