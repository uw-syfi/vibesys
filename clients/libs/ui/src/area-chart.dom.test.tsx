/** The area chart and the share bar in a browser DOM, through the `.` entry only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {areaPath, linePath} from './components/area-chart.js';
import {AreaChart, niceScale, ShareBar} from './index.js';

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

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => createRoot(container).render(node));
  return container;
}

describe('niceScale', () => {
  test('property: the top covers the maximum, ticks start at zero in equal steps, and there are few', () => {
    fc.assert(
      fc.property(
        fc.double({min: 1e-9, max: 1e12, noNaN: true}),
        fc.integer({min: 1, max: 8}),
        (max, count) => {
          const scale = niceScale(max, count);
          expect(scale.top).toBeGreaterThanOrEqual(max * (1 - 1e-9));
          expect(scale.ticks[0]).toBe(0);
          expect(scale.ticks.at(-1)).toBeCloseTo(scale.top, 6);
          const step = (scale.ticks[1] ?? 0) - (scale.ticks[0] ?? 0);
          scale.ticks.forEach((tick, at) => {
            expect(tick).toBeCloseTo(at * step, 6);
          });
          // A tidy step is 1, 2, 2.5 or 5 times a power of ten.
          const mantissa = step / 10 ** Math.floor(Math.log10(step));
          expect([1, 2, 2.5, 5, 10].some(each => Math.abs(each - mantissa) < 1e-6)).toBe(true);
          expect(scale.ticks.length).toBeLessThanOrEqual(2 * count + 2);
        },
      ),
      {numRuns: 300, seed: 3544},
    );
  });

  test('nothing to draw still has an axis', () => {
    for (const max of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(niceScale(max, 4)).toEqual({top: 1, ticks: [0, 1]});
    }
  });
});

describe('linePath', () => {
  test('property: the curve never leaves the range of the points it joins', () => {
    fc.assert(
      fc.property(
        fc.array(fc.double({min: 0, max: 100, noNaN: true}), {minLength: 2, maxLength: 12}),
        values => {
          const points = values.map((y, at) => ({x: at * 10, y}));
          const path = linePath(points);
          // Control points of a monotone cubic stay between their segment's ends in y.
          const numbers = [
            ...path.matchAll(/C([-\d.e]+) ([-\d.e]+) ([-\d.e]+) ([-\d.e]+) ([-\d.e]+) ([-\d.e]+)/g),
          ];
          expect(numbers).toHaveLength(values.length - 1);
          numbers.forEach((match, at) => {
            const low = Math.min(values[at] ?? 0, values[at + 1] ?? 0) - 1e-6;
            const high = Math.max(values[at] ?? 0, values[at + 1] ?? 0) + 1e-6;
            for (const y of [Number(match[2]), Number(match[4])]) {
              expect(y).toBeGreaterThanOrEqual(low);
              expect(y).toBeLessThanOrEqual(high);
            }
          });
        },
      ),
      {numRuns: 200, seed: 3545},
    );
  });

  test('one point, none, and a closed area', () => {
    expect(linePath([])).toBe('');
    expect(linePath([{x: 1, y: 2}])).toBe('M1 2');
    expect(areaPath([], 9)).toBe('');
    expect(
      areaPath(
        [
          {x: 0, y: 5},
          {x: 10, y: 3},
        ],
        9,
      ),
    ).toMatch(/L10 9 L0 9 Z$/);
  });
});

describe('AreaChart', () => {
  test('draws a layer per provider over a tidy axis, names its slices, and a tooltip row per layer', async () => {
    const container = await mount(
      <AreaChart
        label="Cost over time"
        unit="US dollars"
        labels={['Oct 29', 'Oct 30', 'Oct 31']}
        layers={[
          {key: 'a', name: 'Claude', series: 0, values: [1, 4, 2]},
          {key: 'b', name: 'Codex', series: 1, values: [0, 1, 3]},
        ]}
        format={value => `$${value}`}
      />,
    );
    expect(container.querySelector('svg')?.getAttribute('aria-label')).toBe(
      'Cost over time, in US dollars',
    );
    expect(container.querySelectorAll('.vs-area-chart-layer')).toHaveLength(2);
    expect(container.querySelectorAll('.vs-area-chart-slice')).toHaveLength(3);
    expect(container.querySelectorAll('.vs-area-chart-tip-row')).toHaveLength(6);
    const axis = [...container.querySelectorAll('.vs-chart-label')].map(each => each.textContent);
    expect(axis).toContain('$4');
    expect(axis).toContain('Oct 30');
  });

  test('an empty chart still draws its axis', async () => {
    const container = await mount(
      <AreaChart
        label="Cost"
        unit="US dollars"
        labels={[]}
        layers={[]}
        format={value => `${value}`}
      />,
    );
    expect(container.querySelectorAll('.vs-chart-grid').length).toBeGreaterThan(0);
  });
});

describe('ShareBar', () => {
  test('cuts the bar by value and lists every part, even one of nothing', async () => {
    const container = await mount(
      <ShareBar
        label="Cost by type"
        parts={[
          {key: 'in', label: 'Input', value: 3, figure: '$3', series: 0},
          {key: 'out', label: 'Output', value: 1, figure: '$1', series: 1},
          {key: 'none', label: 'Cache', value: 0, figure: '$0', series: 2},
        ]}
      />,
    );
    const segments = [...container.querySelectorAll<HTMLElement>('.vs-share-segment')];
    expect(segments.map(each => each.style.flexGrow)).toEqual(['0.75', '0.25']);
    expect([...container.querySelectorAll('.vs-share-part')].map(each => each.textContent)).toEqual(
      ['Input$3', 'Output$1', 'Cache$0'],
    );
  });
});

describe('AreaChart axis words', () => {
  test('the axis may word a value shorter than the tooltip does', async () => {
    const container = await mount(
      <AreaChart
        label="Spend"
        unit="US dollars"
        labels={['a', 'b']}
        layers={[{key: 'x', name: 'x', series: 0, values: [100, 300]}]}
        format={value => `$${value}.00`}
        axis={value => `$${value}`}
      />,
    );
    const axis = [...container.querySelectorAll('.vs-chart-label')].map(each => each.textContent);
    expect(axis).toContain('$300');
    expect(axis).not.toContain('$300.00');
    const tips = [...container.querySelectorAll('.vs-area-chart-tip-row')].map(
      each => each.textContent,
    );
    expect(tips).toEqual(['x $100.00', 'x $300.00']);
  });
});
