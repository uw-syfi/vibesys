import {type JSX, useCallback, useRef, useState} from 'react';
import {SIZE} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';

/** One arrow-key press moves the separator this many pixels. */
const STEP = 16;

/**
 * A pane the user can resize (a `Pane`, or a `Dock`'s panel): the width they gave it, and where a
 * new one goes once they let go. The separator at the pane's edge moves within the `pane-min` and
 * `pane-max` tokens, or from wherever the layout drew the pane outside them; the layout keeps the
 * pane within its row, and at least `pane-min` beside it.
 */
export interface PaneResize {
  /** The width the user gave the pane, in pixels; null keeps its default width. */
  readonly width: number | null;
  /** Called once a drag or a key is let go, with the width it ended at. */
  readonly changeWidth: (width: number) => void;
  /** The separator's accessible name: "Resize the ideas list". */
  readonly label: string;
  /** A width in words, for assistive technology: "400 pixels wide". */
  readonly widthWords: (width: number) => string;
  /** The pane's edge the separator sits at: its end (the default, the pane on the left) or start. */
  readonly edge?: 'start' | 'end';
}

/** A width to the nearest whole pixel. */
const wholePixels = (width: number): number => Math.floor(width + 0.5);

/**
 * The width an element is drawn at, border included, as it changes, and the ref that names the
 * element; null until measured and wherever there is no layout. It sets no timer: it reads the
 * browser's own resize notice.
 */
function useDrawnWidth(): readonly [
  (element: HTMLElement | null) => (() => void) | undefined,
  number | null,
] {
  const [width, setWidth] = useState<number | null>(null);
  const measure = useCallback((element: HTMLElement | null) => {
    if (element === null || typeof ResizeObserver === 'undefined') return undefined;
    const read = (): void => setWidth(wholePixels(element.getBoundingClientRect().width) || null);
    read();
    const watch = new ResizeObserver(read);
    watch.observe(element);
    return () => watch.disconnect();
  }, []);
  return [measure, width];
}

/**
 * What a resizable pane's element takes (its measure, the width it is drawn at as a custom
 * property, and whether it has one) and the separator to draw at its edge, or nothing without
 * `resize`. While a drag or a key is held the pane follows it here; the width goes to
 * `changeWidth` when it is let go. The separator reads the width the pane is drawn at, so a step or
 * a drag starts where the pane is, whatever bound the layout put on it.
 */
export function useResizable(
  resize: PaneResize | undefined,
  id: string,
): {
  readonly measure: ReturnType<typeof useDrawnWidth>[0] | undefined;
  readonly style: StyleWithVars | undefined;
  readonly sized: boolean;
  readonly separator: JSX.Element | null;
} {
  const [measure, drawn] = useDrawnWidth();
  const [moving, setMoving] = useState<number | null>(null);
  if (resize === undefined)
    return {measure: undefined, style: undefined, sized: false, separator: null};
  const min = SIZE['pane-min'];
  const asked = moving ?? resize.width;
  return {
    measure,
    style: asked === null ? undefined : {'--pane-size': `${asked}px`},
    sized: asked !== null,
    separator: (
      <PaneSeparator
        label={resize.label}
        width={drawn ?? asked ?? min}
        min={min}
        max={SIZE['pane-max']}
        sized={resize.edge === 'start' ? 'after' : 'before'}
        widthWords={resize.widthWords}
        moveWidth={setMoving}
        changeWidth={width => {
          resize.changeWidth(width);
          setMoving(null);
        }}
        controls={id}
      />
    ),
  };
}

/** The width a key asks for, or null for a key the separator does not take. */
function keyWidth(
  key: string,
  width: number,
  min: number,
  max: number,
  grows: number,
): number | null {
  switch (key) {
    case 'Home':
      return min;
    case 'End':
      return max;
    case 'ArrowRight':
      return width + STEP * grows;
    case 'ArrowLeft':
      return width - STEP * grows;
    default:
      return null;
  }
}

/**
 * The narrowest and widest a separator moves a pane drawn at `width` to: `min` and `max`, or the
 * drawn width where the layout put the pane outside them, so no move goes against its key or
 * pointer and the bounds it reports are the ones it keeps.
 */
function boundsAt(width: number, min: number, max: number): {lo: number; hi: number} {
  return {lo: Math.min(min, wholePixels(width)), hi: Math.max(max, wholePixels(width))};
}

/**
 * The handle between two panes side by side that sets the width of one of them, the pane
 * `sized` names: the one `before` it or the one `after` it. Drag it, or focus it and press Left
 * or Right (a step), Home (narrowest) or End (widest). It keeps no width of its own: `width` is the
 * sized pane's width as drawn, in pixels. While a drag or a key is held each move goes to
 * `moveWidth`; letting go gives the last to `changeWidth`. For assistive technology it is a
 * focusable separator whose value is that width, in `widthWords`.
 */
export function PaneSeparator(props: {
  /** What it resizes, as a person would say it: "Resize the ideas list". */
  readonly label: string;
  readonly width: number;
  readonly min: number;
  readonly max: number;
  readonly sized: 'before' | 'after';
  readonly widthWords: (width: number) => string;
  /** The width a held drag or key is at; null when it was cancelled. */
  readonly moveWidth: (width: number | null) => void;
  readonly changeWidth: (width: number) => void;
  /** The id of the pane it sizes. */
  readonly controls?: string;
}): JSX.Element {
  const {moveWidth, changeWidth} = props;
  const {lo, hi} = boundsAt(props.width, props.min, props.max);
  const now = wholePixels(props.width);
  // Moving toward the sized pane narrows it.
  const grows = props.sized === 'before' ? 1 : -1;
  const drag = useRef<{
    readonly x: number;
    readonly width: number;
    readonly lo: number;
    readonly hi: number;
  } | null>(null);
  // The width the held drag or key last asked for, given to `changeWidth` when it is let go.
  const held = useRef<number | null>(null);
  const hold = (width: number, from: {readonly lo: number; readonly hi: number}): void => {
    held.current = wholePixels(Math.max(from.lo, Math.min(from.hi, width)));
    moveWidth(held.current);
  };
  const letGo = (): void => {
    drag.current = null;
    const width = held.current;
    held.current = null;
    if (width !== null) changeWidth(width);
  };
  return (
    <hr
      className="vs-pane-separator"
      aria-label={props.label}
      aria-orientation="vertical"
      aria-valuemin={lo}
      aria-valuemax={hi}
      aria-valuenow={now}
      aria-valuetext={props.widthWords(now)}
      aria-controls={props.controls}
      tabIndex={0}
      onPointerDown={event => {
        if (event.button !== 0) return;
        drag.current = {x: event.clientX, width: props.width, lo, hi};
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={event => {
        const start = drag.current;
        if (start !== null) hold(start.width + (event.clientX - start.x) * grows, start);
      }}
      onPointerUp={letGo}
      onPointerCancel={() => {
        drag.current = null;
        held.current = null;
        moveWidth(null);
      }}
      onKeyDown={event => {
        const next = keyWidth(event.key, held.current ?? props.width, lo, hi, grows);
        if (next === null) return;
        event.preventDefault();
        hold(next, {lo, hi});
      }}
      onKeyUp={letGo}
      onBlur={letGo}
    />
  );
}
