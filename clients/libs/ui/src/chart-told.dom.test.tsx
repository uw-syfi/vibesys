/**
 * What a chart draws of what it is told rather than works out: a reference with its provenance, a
 * point that only names itself, and a Pareto frontier the caller's source decided. Through the `.`
 * entry only.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {Chart, type ParetoAxis, ParetoChart, type ParetoPoint} from './index.js';

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

describe('Chart references', () => {
  const points = [
    {id: 'a', x: 1, y: 10},
    {id: 'b', x: 2, y: 20},
  ];

  test('a reference with a hint is a focusable label that carries the hint, and is not also drawn as text', async () => {
    const root = await mount(
      <Chart
        label="c"
        points={points}
        references={[{y: 30, label: 'Bound 30', hint: '30 tok/s\nlead, derived from bandwidth'}]}
      />,
    );
    const label = root.querySelector('.vs-chart-reference-label');
    expect(label?.textContent).toBe('Bound 30');
    expect(label?.getAttribute('aria-description')).toBe('30 tok/s\nlead, derived from bandwidth');
    expect(label?.getAttribute('tabindex')).toBe('0');
    expect(root.querySelectorAll('.vs-chart-reference')).toHaveLength(1);
    expect(root.querySelector('svg text[class="vs-chart-label"]')?.textContent ?? '').not.toBe(
      'Bound 30',
    );
  });

  test('a reference without a hint is drawn text only, as before', async () => {
    const root = await mount(
      <Chart label="c" points={points} references={[{y: 30, label: 'Bound 30'}]} />,
    );
    expect(root.querySelector('.vs-chart-reference-label')).toBeNull();
    expect(
      [...root.querySelectorAll('svg text')].some(text => text.textContent === 'Bound 30'),
    ).toBe(true);
  });

  test('the label stands where the line is: the same pixel height for any value', async () => {
    await fc.assert(
      fc.asyncProperty(fc.double({min: 0, max: 100, noNaN: true}), async value => {
        const root = await mount(
          <Chart
            label="c"
            points={points}
            yDomain={[0, 100]}
            references={[{y: value, label: 'B', hint: 'h'}]}
          />,
        );
        const line = root.querySelector('line.vs-chart-reference');
        const label = root.querySelector<HTMLElement>('.vs-chart-reference-label');
        expect(label?.style.getPropertyValue('--reference-top')).toBe(
          `${line?.getAttribute('y1')}px`,
        );
      }),
      {numRuns: 25, seed: 4050},
    );
  });
});

describe('Chart label room', () => {
  const axisLeft = (root: HTMLElement): string | null | undefined =>
    root.querySelector('line.vs-chart-axis')?.getAttribute('x1');

  test('property: charts told the same room start their plots at one left edge, whatever their labels', () => {
    return fc.assert(
      fc.asyncProperty(fc.integer({min: 1, max: 6}), fc.integer({min: 1, max: 6}), async (a, b) => {
        const ticks = (chars: number) => [{value: 1, label: '1'.repeat(chars)}];
        const room = Math.max(a, b);
        const first = await mount(
          <Chart label="a" points={[{x: 1, y: 1}]} yTicks={ticks(a)} yLabelChars={room} />,
        );
        const second = await mount(
          <Chart label="b" points={[{x: 1, y: 1}]} yTicks={ticks(b)} yLabelChars={room} />,
        );
        expect(axisLeft(first)).toBe(axisLeft(second));
      }),
      {numRuns: 25, seed: 4052},
    );
  });

  test('a label longer than the room still gets its own', async () => {
    const roomy = await mount(
      <Chart label="a" points={[{x: 1, y: 1}]} yTicks={[{value: 1, label: '1'}]} yLabelChars={2} />,
    );
    const long = await mount(
      <Chart
        label="b"
        points={[{x: 1, y: 1}]}
        yTicks={[{value: 1, label: '1'.repeat(8)}]}
        yLabelChars={2}
      />,
    );
    expect(Number(axisLeft(long))).toBeGreaterThan(Number(axisLeft(roomy)));
  });
});

describe('Chart points that open nothing', () => {
  test('say what they are on hover and are not controls', async () => {
    const root = await mount(
      <Chart
        label="c"
        points={[{id: 'job-1', x: 1, y: 2, tip: {lead: 'c-214', detail: '2.31 tok/s'}}]}
      />,
    );
    expect(root.querySelectorAll('[role="button"]')).toHaveLength(0);
    const point = root.querySelector('circle.vs-chart-point')?.parentElement;
    await act(async () => {
      point?.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
    });
    expect(root.querySelector('.vs-chart-tip-text')?.textContent).toBe('c-214 · 2.31 tok/s');
    await act(async () => {
      point?.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    });
    expect(root.querySelector('.vs-chart-tip-text')).toBeNull();
  });
});

describe('ParetoChart with a frontier it is told', () => {
  const x: ParetoAxis = {name: 'Throughput', unit: 'tok/s', direction: 'higher'};
  const y: ParetoAxis = {name: 'Latency', unit: 'ms', direction: 'lower'};
  const roles = (root: HTMLElement): Record<string, string> =>
    Object.fromEntries(
      [...root.querySelectorAll('.vs-chart .vs-pareto-point')].map(point => [
        point.querySelector('title')?.textContent?.split(' · ')[0] ?? '',
        point.getAttribute('data-role') ?? '',
      ]),
    );

  test('draws exactly the points told to be on it, whatever the two axes say', async () => {
    const points: ParetoPoint[] = [
      // `b` is beaten on both axes by `a`, and told to be on the frontier (a third objective).
      {id: 'a', label: 'a', x: 10, y: 5, time: 1, state: 'passed', onFrontier: true},
      {id: 'b', label: 'b', x: 5, y: 9, time: 2, state: 'passed', onFrontier: true},
      {id: 'c', label: 'c', x: 12, y: 4, time: 3, state: 'passed', onFrontier: false},
    ];
    const root = await mount(<ParetoChart label="t" points={points} x={x} y={y} joined={false} />);
    expect(roles(root)).toEqual({a: 'frontier', b: 'frontier', c: 'dominated'});
    expect(root.querySelector('.vs-pareto-frontier')).toBeNull();
  });

  test('property: the frontier drawn is the set the points say, for any marks', () => {
    return fc.assert(
      fc.asyncProperty(fc.array(fc.boolean(), {minLength: 1, maxLength: 12}), async marks => {
        const points: ParetoPoint[] = marks.map((onFrontier, index) => ({
          id: `p${index}`,
          label: `p${index}`,
          x: index,
          y: index % 3,
          time: index,
          state: 'passed',
          onFrontier,
        }));
        const root = await mount(<ParetoChart label="t" points={points} x={x} y={y} />);
        const drawn = Object.entries(roles(root))
          .filter(([, role]) => role === 'frontier')
          .map(([name]) => name)
          .sort();
        expect(drawn).toEqual(
          points
            .filter(p => p.onFrontier === true)
            .map(p => p.id)
            .sort(),
        );
      }),
      {numRuns: 25, seed: 4051},
    );
  });

  test('joins the frontier by a staircase unless told not to', async () => {
    const points: ParetoPoint[] = [
      {id: 'a', label: 'a', x: 10, y: 5, time: 1, state: 'passed', onFrontier: true},
      {id: 'b', label: 'b', x: 5, y: 9, time: 2, state: 'passed', onFrontier: false},
    ];
    const joined = await mount(<ParetoChart label="t" points={points} x={x} y={y} />);
    expect(joined.querySelector('.vs-pareto-frontier')).not.toBeNull();
  });
});
