import {useCallback, useState} from 'react';
import {MONO_ADVANCE, TYPE} from '../tokens/index.js';

/** A label to place along an axis: `share` is where it points, from 0 (start) to 1 (end). */
export interface AxisLabel {
  readonly share: number;
  readonly text: string;
}

/** A label that fits: its index in the input and the pixel its text starts at. */
export interface PlacedLabel {
  readonly index: number;
  readonly left: number;
}

/** A conservative glyph advance for the tracked `section` style every axis label uses. */
const SECTION_GLYPH = TYPE.section.size * (MONO_ADVANCE + TYPE.section.tracking);
/** The air kept between two labels on one axis. */
const LABEL_GAP = 8;

interface Box {
  readonly index: number;
  readonly left: number;
  readonly right: number;
}

function boxOf(label: AxisLabel, index: number, width: number, glyph: number): Box {
  const own = label.text.length * glyph;
  const centred = label.share * width - own / 2;
  const left = Math.min(Math.max(centred, 0), Math.max(width - own, 0));
  return {index, left, right: left + own};
}

/**
 * The labels of an axis `width` pixels wide that can all be read: none overlaps another, each is
 * kept inside the axis (a label near an end slides inward instead of hanging off it), and the
 * first and last labels are kept whenever they fit with each other. `labels` come in order along
 * the axis; the ones that would collide with a kept neighbour are dropped, not shrunk.
 */
export function placeLabels(
  labels: readonly AxisLabel[],
  width: number,
  glyph: number = SECTION_GLYPH,
  gap: number = LABEL_GAP,
): PlacedLabel[] {
  const boxes = labels.map((label, index) => boxOf(label, index, width, glyph));
  const kept: Box[] = [];
  for (const box of boxes) {
    const last = kept.at(-1);
    if (last === undefined || box.left >= last.right + gap) kept.push(box);
  }
  const end = boxes.at(-1);
  if (end !== undefined && kept.at(-1)?.index !== end.index) {
    while (kept.length > 1 && (kept.at(-1)?.right ?? 0) + gap > end.left) kept.pop();
    const before = kept.at(-1);
    if (before === undefined || before.right + gap <= end.left) kept.push(end);
  }
  return kept.map(box => ({index: box.index, left: box.left}));
}

/**
 * Whether every label of an axis `width` pixels wide can be drawn centred on its own share: none
 * reaches past either end and none comes within `gap` of its neighbour. `labels` come in order.
 */
export function labelsFit(
  labels: readonly AxisLabel[],
  width: number,
  glyph: number = SECTION_GLYPH,
  gap: number = LABEL_GAP,
): boolean {
  let right = Number.NEGATIVE_INFINITY;
  for (const label of labels) {
    const own = label.text.length * glyph;
    const left = label.share * width - own / 2;
    if (left < 0 || left + own > width || left < right + gap) return false;
    right = left + own;
  }
  return true;
}

/** A zero width is no layout at all (a hidden element, a test renderer): treat it as unmeasured. */
function measured(width: number): number | null {
  return width > 0 ? width : null;
}

type Measure = (element: HTMLElement | null) => (() => void) | undefined;

/**
 * The inline size of an element in pixels as it changes, and the ref that names the element. The
 * size is null until it is measured and wherever there is no layout to measure (a server render);
 * a caller then draws without fitting. It sets no timer: it reads the browser's own resize notice.
 */
export function useInlineSize(): readonly [Measure, number | null] {
  const [width, setWidth] = useState<number | null>(null);
  const measure = useCallback<Measure>(element => {
    if (element === null || typeof ResizeObserver === 'undefined') return undefined;
    setWidth(measured(element.getBoundingClientRect().width));
    const watch = new ResizeObserver(entries => {
      const entry = entries.at(-1);
      if (entry !== undefined) setWidth(measured(entry.contentRect.width));
    });
    watch.observe(element);
    return () => watch.disconnect();
  }, []);
  return [measure, width];
}

/** The size of a box in pixels, as measured. */
export interface BoxSize {
  readonly width: number;
  readonly height: number;
}

function sizeOf(width: number, height: number): BoxSize | null {
  return width > 0 && height > 0 ? {width, height} : null;
}

/**
 * The size of an element in pixels as it changes, for a drawing that fills a box its stylesheet
 * sizes (a height set by the window's, say) and draws itself to that box. Like `useInlineSize`: null
 * until measured and wherever there is no layout, and no timer.
 */
export function useBoxSize(): readonly [Measure, BoxSize | null] {
  const [size, setSize] = useState<BoxSize | null>(null);
  const measure = useCallback<Measure>(element => {
    if (element === null || typeof ResizeObserver === 'undefined') return undefined;
    const rect = element.getBoundingClientRect();
    setSize(sizeOf(rect.width, rect.height));
    const watch = new ResizeObserver(entries => {
      const entry = entries.at(-1);
      if (entry !== undefined) setSize(sizeOf(entry.contentRect.width, entry.contentRect.height));
    });
    watch.observe(element);
    return () => watch.disconnect();
  }, []);
  return [measure, size];
}
