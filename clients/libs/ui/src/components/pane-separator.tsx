import {type JSX, type PointerEvent, useCallback, useRef, useState} from 'react';
import {SIZE} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';

/** One arrow-key press moves the separator this many pixels. */
const STEP = 16;

/**
 * A pane the user can resize (a `Pane`, or a `Dock`'s panel): the width they gave it, and where a
 * new one goes. The component draws the separator at the pane's edge and keeps every width
 * within the `pane-min` and `pane-max` tokens; the pane also keeps at least `pane-min` beside it.
 */
export interface PaneResize {
  /** The width the user gave the pane, in pixels; null keeps its default width. */
  readonly width: number | null;
  readonly changeWidth: (width: number) => void;
  /** The separator's accessible name: "Resize the ideas list". */
  readonly label: string;
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
 * What a resizable pane's element takes (its measure, the width the user gave it as a custom
 * property, and whether it has one) and the separator to draw at its edge, or nothing to add
 * without `resize`. The separator reads the width the pane is drawn at, so a step or a drag starts
 * where the pane is, whatever bound the layout put on it.
 */
export function useResizable(
  resize: PaneResize | undefined,
  id: string,
): {
  readonly measure: ReturnType<typeof useDrawnWidth>[0];
  readonly style: StyleWithVars | undefined;
  readonly sized: boolean;
  readonly separator: JSX.Element | null;
} {
  const [measure, drawn] = useDrawnWidth();
  const min = SIZE['pane-min'];
  const max = SIZE['pane-max'];
  if (resize === undefined) return {measure, style: undefined, sized: false, separator: null};
  const asked = resize.width === null ? null : Math.max(min, Math.min(max, resize.width));
  return {
    measure,
    style: asked === null ? undefined : {'--pane-size': `${asked}px`},
    sized: asked !== null,
    separator: (
      <PaneSeparator
        label={resize.label}
        width={drawn ?? asked ?? min}
        min={min}
        max={max}
        sized={resize.edge === 'start' ? 'after' : 'before'}
        changeWidth={resize.changeWidth}
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
 * The handle between two panes side by side that sets the width of one of them, the pane
 * `sized` names: the one `before` it or the one `after` it. Drag it, or focus it and press Left
 * or Right (a step), Home (narrowest) or End (widest). It keeps no width of its own: `width` is the
 * sized pane's width as drawn, in pixels, and every move asks `changeWidth` for a width within
 * `min` and `max`. For assistive technology it is a focusable separator whose value is that width.
 */
export function PaneSeparator(props: {
  /** What it resizes, as a person would say it: "Resize the ideas list". */
  readonly label: string;
  readonly width: number;
  readonly min: number;
  readonly max: number;
  readonly sized: 'before' | 'after';
  readonly changeWidth: (width: number) => void;
  /** The id of the pane it sizes. */
  readonly controls?: string;
}): JSX.Element {
  const {min, max, changeWidth} = props;
  const within = (width: number): number => wholePixels(Math.max(min, Math.min(max, width)));
  // Moving toward the sized pane narrows it.
  const grows = props.sized === 'before' ? 1 : -1;
  const drag = useRef<{readonly x: number; readonly width: number} | null>(null);
  const move = (event: PointerEvent<HTMLHRElement>): void => {
    const start = drag.current;
    if (start !== null) changeWidth(within(start.width + (event.clientX - start.x) * grows));
  };
  return (
    <hr
      className="vs-pane-separator"
      aria-label={props.label}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={within(props.width)}
      aria-controls={props.controls}
      tabIndex={0}
      onPointerDown={event => {
        if (event.button !== 0) return;
        drag.current = {x: event.clientX, width: props.width};
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={move}
      onPointerUp={() => {
        drag.current = null;
      }}
      onPointerCancel={() => {
        drag.current = null;
      }}
      onKeyDown={event => {
        const next = keyWidth(event.key, props.width, min, max, grows);
        if (next === null) return;
        event.preventDefault();
        changeWidth(within(next));
      }}
    />
  );
}
