import type {JSX} from 'react';
import {TYPE} from '../tokens/index.js';
import {NewBelowSlot} from './new-below.js';
import {ScrollArea} from './scroll-area.js';
import {useWindowed} from './windowing.js';

/** One line of output; `err` lines (standard error) are drawn in `err`. */
export interface LogLine {
  readonly text: string;
  readonly stream?: 'out' | 'err';
}

/**
 * Monospace output on the log background in every theme. Only the lines near the visible part are
 * drawn, between spacers as tall as the rest, so a log of any length costs a screenful of lines;
 * a line's height is one code line until it has been drawn (a wrapped line is measured). While
 * scrolled to the bottom it follows the tail as lines arrive; scrolling up stops following and
 * shows a "New below" pill that jumps to the tail. Lines are text, never HTML.
 */
export function LogPane(props: {
  readonly label: string;
  readonly lines: readonly LogLine[];
  /** `fill` takes the height of its container (a pane body); the others have fixed heights. */
  readonly size?: 'sm' | 'md' | 'fill';
  /**
   * Where it opens: `tail` (the default) follows the end as lines arrive; `top` opens at the first
   * line and does not follow, for a log that is finished.
   */
  readonly start?: 'tail' | 'top';
}): JSX.Element {
  const {lines} = props;
  const win = useWindowed({
    count: lines.length,
    // Lines only append, so a line's index is its identity.
    keyOf: index => String(index),
    estimate: TYPE.code.line,
    follow: props.start !== 'top',
  });
  return (
    <div className="vs-log" data-size={props.size ?? 'md'}>
      <ScrollArea
        as="pre"
        kind="feed"
        ref={win.scroller}
        className="vs-log-pane"
        data-size={props.size ?? 'md'}
        role="log"
        aria-label={props.label}
        tabIndex={0}
        onScroll={win.scrolled}
      >
        <span className="vs-log-gap" style={{height: win.before}} />
        {lines.slice(win.first, win.last).map((line, offset) => (
          <span
            // biome-ignore lint/suspicious/noArrayIndexKey: lines only append, so a line's index is its identity.
            key={win.first + offset}
            ref={win.slotOf(win.first + offset).measure}
            className="vs-log-line"
            data-stream={line.stream ?? 'out'}
          >
            {line.text}
          </span>
        ))}
        <span className="vs-log-gap" style={{height: win.after}} />
      </ScrollArea>
      {win.behind ? <NewBelowSlot text="New below" press={win.jump} /> : null}
    </div>
  );
}
