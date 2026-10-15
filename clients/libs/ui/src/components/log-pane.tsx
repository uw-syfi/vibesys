import {type JSX, type UIEvent, useRef, useState} from 'react';
import {Button} from './button.js';

/** One line of output; `err` lines (standard error) are drawn in `err`. */
export interface LogLine {
  readonly text: string;
  readonly stream?: 'out' | 'err';
}

/** Whether the pane shows its last line, allowing for a subpixel scroll offset. */
function atBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 2;
}

/**
 * Monospace output on the log background in every theme. While scrolled to the bottom it follows
 * the tail as lines arrive; scrolling up stops following and offers "Jump to latest". Lines are
 * text, never HTML.
 *
 * The ref callback is a new function on every render, so React calls it after every commit: the
 * pane scrolls to the new tail without an effect hook.
 */
export function LogPane(props: {
  readonly label: string;
  readonly lines: readonly LogLine[];
  readonly size?: 'sm' | 'md';
}): JSX.Element {
  const following = useRef(true);
  const pane = useRef<HTMLPreElement | null>(null);
  const [behind, setBehind] = useState(false);
  const follow = (element: HTMLPreElement | null): void => {
    pane.current = element;
    if (element !== null && following.current) element.scrollTop = element.scrollHeight;
  };
  const scrolled = (event: UIEvent<HTMLPreElement>): void => {
    following.current = atBottom(event.currentTarget);
    if (behind === following.current) setBehind(!following.current);
  };
  const jump = (): void => {
    following.current = true;
    setBehind(false);
    if (pane.current !== null) pane.current.scrollTop = pane.current.scrollHeight;
  };
  return (
    <div className="vs-log">
      <pre
        ref={follow}
        className="vs-log-pane"
        data-size={props.size ?? 'md'}
        role="log"
        aria-label={props.label}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrolling box must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls text.
        tabIndex={0}
        onScroll={scrolled}
      >
        {props.lines.map((line, index) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: lines only append, so a line's index is its identity.
          <span key={index} className="vs-log-line" data-stream={line.stream ?? 'out'}>
            {line.text}
            {'\n'}
          </span>
        ))}
      </pre>
      {behind ? (
        <span className="vs-log-jump">
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
