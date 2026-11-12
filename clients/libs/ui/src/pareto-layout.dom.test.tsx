/**
 * Where a Pareto chart puts its words and its frontier: nothing the chart writes overlaps a point,
 * leaves the drawing, or runs out to the frame. Read from the drawn SVG through the `.` entry only.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {type ParetoAxis, ParetoChart, type ParetoPoint} from './index.js';
import {MONO_ADVANCE, TYPE} from './tokens/index.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

/** The conservative advance the chart uses for one glyph of its tracked section labels. */
const GLYPH = TYPE.section.size * (MONO_ADVANCE + TYPE.section.tracking);
/** A mark's reach around its centre, with the ring a highlighted point has. */
const REACH = 10;

interface Box {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
}

const meets = (a: Box, b: Box): boolean =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

async function draw(
  points: readonly ParetoPoint[],
  x: ParetoAxis,
  y: ParetoAxis,
): Promise<SVGSVGElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () =>
    createRoot(container).render(<ParetoChart label="Trade-off" points={points} x={x} y={y} />),
  );
  const svg = container.querySelector('svg.vs-chart');
  if (svg === null) throw new Error('no chart drawn');
  return svg as SVGSVGElement;
}

const num = (element: Element | null, name: string): number => Number(element?.getAttribute(name));

/** The words written on the chart, as boxes: `anchor` end words grow left from their x. */
function wordsOf(svg: SVGSVGElement, selector: string): Box[] {
  return [...svg.querySelectorAll(selector)].map(text => {
    const width = (text.textContent?.length ?? 0) * GLYPH;
    const x = num(text, 'x');
    const y = num(text, 'y');
    const left = text.getAttribute('text-anchor') === 'end' ? x - width : x;
    return {left, right: left + width, top: y - 12, bottom: y + 4};
  });
}

const marksOf = (svg: SVGSVGElement): Box[] =>
  [...svg.querySelectorAll('.vs-pareto-mark')].map(mark => {
    const circle = mark.tagName.toLowerCase() === 'circle';
    const cx = circle ? num(mark, 'cx') : num(mark, 'x') + num(mark, 'width') / 2;
    const cy = circle ? num(mark, 'cy') : num(mark, 'y') + num(mark, 'height') / 2;
    return {left: cx - REACH, right: cx + REACH, top: cy - REACH, bottom: cy + REACH};
  });

const frameOf = (svg: SVGSVGElement): Box => {
  const frame = svg.querySelector('.vs-chart-frame');
  const left = num(frame, 'x');
  const top = num(frame, 'y');
  return {left, top, right: left + num(frame, 'width'), bottom: top + num(frame, 'height')};
};

const axis = fc.record({
  name: fc.constantFrom('Throughput', 'Latency', 'Memory'),
  unit: fc.constantFrom('', 'ops/s', 'ms', 'GB'),
  direction: fc.constantFrom('higher' as const, 'lower' as const),
});

const pointsOf = fc
  .uniqueArray(
    fc.record({x: fc.integer({min: 1, max: 5000}), y: fc.integer({min: 1, max: 5000})}),
    {minLength: 2, maxLength: 14, selector: p => `${p.x}:${p.y}`},
  )
  .map((raw): ParetoPoint[] =>
    raw.map((p, index) => ({
      id: `p${index}`,
      x: p.x,
      y: p.y,
      time: index,
      state: 'passed',
    })),
  );

describe('the words and the frontier of a Pareto chart', () => {
  test('`Better` sits above the frame at its right, clear of every point and of the y title', async () => {
    await fc.assert(
      fc.asyncProperty(pointsOf, axis, axis, async (points, x, y) => {
        const svg = await draw(points, x, y);
        const better = wordsOf(svg, '.vs-pareto-better');
        const frame = frameOf(svg);
        for (const word of better) {
          expect(word.bottom).toBeLessThanOrEqual(frame.top);
          expect(word.right).toBeLessThanOrEqual(frame.right);
          for (const mark of marksOf(svg)) expect(meets(word, mark)).toBe(false);
          const title = wordsOf(svg, '.vs-chart-label').find(each => each.top === word.top);
          if (title !== undefined) expect(title.right).toBeLessThan(word.left);
        }
        svg.parentElement?.parentElement?.remove();
      }),
      {numRuns: 40},
    );
  });

  test('no word leaves the drawing, and the tick words under the plot stay within it and apart', async () => {
    await fc.assert(
      fc.asyncProperty(pointsOf, axis, axis, async (points, x, y) => {
        const svg = await draw(points, x, y);
        const [, , width, height] = (svg.getAttribute('viewBox') ?? '').split(' ').map(Number);
        const frame = frameOf(svg);
        const inside = (box: Box): boolean =>
          box.left >= 0 && box.right <= (width ?? 0) && box.top >= 0 && box.bottom <= (height ?? 0);
        const labels = wordsOf(svg, 'text.vs-chart-label');
        for (const box of labels) expect(inside(box)).toBe(true);
        const ticks = labels.filter(
          box => box.top > frame.bottom && box.bottom < (height ?? 0) - 12,
        );
        ticks.sort((a, b) => a.left - b.left);
        for (const [index, tick] of ticks.entries()) {
          expect(tick.left).toBeGreaterThanOrEqual(frame.left);
          expect(tick.right).toBeLessThanOrEqual(frame.right);
          const next = ticks[index + 1];
          if (next !== undefined) expect(tick.right).toBeLessThan(next.left);
        }
        svg.parentElement?.parentElement?.remove();
      }),
      {numRuns: 40},
    );
  });

  test('the frontier is a step from its first point to its last, inside the plot, never out to the frame', async () => {
    await fc.assert(
      fc.asyncProperty(pointsOf, axis, axis, async (points, x, y) => {
        const svg = await draw(points, x, y);
        const d = svg.querySelector('.vs-pareto-frontier')?.getAttribute('d') ?? '';
        const frontier = [
          ...svg.querySelectorAll('.vs-pareto-point[data-role="frontier"] .vs-pareto-mark'),
        ];
        if (d === '') {
          expect(frontier.length).toBeLessThan(2);
        } else {
          const frame = frameOf(svg);
          const start = [...d.matchAll(/M(-?[\d.]+) (-?[\d.]+)/g)][0];
          // It starts on a frontier point, and every corner is inside the frame.
          expect(frontier.map(mark => num(mark, 'cx'))).toContain(Number(start?.[1]));
          for (const px of [Number(start?.[1]), ...coordinates(d, 'H')]) {
            expect(px).toBeGreaterThan(frame.left);
            expect(px).toBeLessThan(frame.right);
          }
          for (const py of [Number(start?.[2]), ...coordinates(d, 'V')]) {
            expect(py).toBeGreaterThan(frame.top);
            expect(py).toBeLessThan(frame.bottom);
          }
        }
        svg.parentElement?.parentElement?.remove();
      }),
      {numRuns: 40},
    );
  });
});

/** The numbers after each `letter` command of a path. */
const coordinates = (d: string, letter: string): number[] =>
  [...d.matchAll(new RegExp(`${letter}(-?[\\d.]+)`, 'g'))].map(match => Number(match[1]));
