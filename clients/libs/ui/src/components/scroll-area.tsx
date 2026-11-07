import {createElement, type HTMLAttributes, type JSX, type Ref} from 'react';

/**
 * What a `ScrollArea` is for, which decides the axis it scrolls along:
 * - `column`: the main column of a page, a pane or a dialog; vertical.
 * - `feed`: a log, a conversation or a list of rows that grows or is windowed; vertical.
 * - `table`: a wide table whose columns cannot wrap any narrower; horizontal. Use it only where
 *   the content truly cannot wrap, and give the table the widest cell it needs and no more.
 */
export type ScrollKind = 'column' | 'feed' | 'table';

/** The elements a `ScrollArea` may be: a list or a section keeps its meaning while it scrolls. */
export type ScrollTag = 'div' | 'section' | 'main' | 'ul' | 'ol' | 'pre';

export interface ScrollAreaProps extends HTMLAttributes<HTMLElement> {
  readonly kind: ScrollKind;
  /** The element drawn; `div` when absent. */
  readonly as?: ScrollTag;
  readonly ref?: Ref<HTMLElement>;
}

/**
 * The only component that scrolls. Every other box in the design system keeps `overflow` visible or
 * clipped, so a scroll bar can appear only on a region that was declared as one, along the one axis
 * its kind scrolls, and only when its content is longer than it is. `data-scroll` (set here only)
 * is what `scroll.css` keys on. Unenforced: no lint or layout check holds this yet, so a new
 * `overflow: auto` or a lone `overflow-y: hidden` (which makes the other axis `auto`) in a stylesheet
 * is caught only by review.
 *
 * A scrolling box takes the keyboard (`tabIndex`) when it has no focusable content of its own, so
 * a keyboard can scroll it (WCAG 2.1.1): the caller passes `tabIndex={0}` then.
 */
export function ScrollArea(props: ScrollAreaProps): JSX.Element {
  const {kind, as = 'div', ref, children, ...rest} = props;
  return createElement(as, {...rest, ref, 'data-scroll': kind}, children);
}
